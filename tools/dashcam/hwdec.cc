// Standalone Qualcomm msm_vidc HEVC decoder for offline dash cam playback.
//
// Based on the current openpilot hardware decoder
// (system/loggerd/encoder/v4l_decoder.cc), but with no libav / msgq dependency:
//   - ION buffers are allocated directly via /dev/ion.
//   - Input is a stream of length-prefixed HEVC access units on stdin.
//   - Output is a stream of length-prefixed, tightly packed NV12 frames on stdout.
//
// The decoder has pipeline latency, so input is read on a separate thread while
// the main loop feeds the hardware and emits decoded frames as they complete.
//
// Protocol (all integers little-endian u32):
//   stdin : [len][payload]   repeated; len == 0 means flush + exit
//   stdout: [width][height][len][payload]  repeated
//           width == 0 signals an error (message on stderr)
//
// Build: g++ -O2 -o hwdec hwdec.cc -lpthread
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <condition_variable>
#include <deque>
#include <memory>
#include <mutex>
#include <atomic>
#include <thread>
#include <vector>

#include <linux/ion.h>
#include <linux/msm_ion.h>
#include <linux/v4l2-controls.h>
#include <linux/videodev2.h>

#define V4L2_EVENT_MSM_VIDC_START (V4L2_EVENT_PRIVATE_START + 0x00001000)
#define V4L2_EVENT_MSM_VIDC_FLUSH_DONE (V4L2_EVENT_MSM_VIDC_START + 1)
#define V4L2_EVENT_MSM_VIDC_PORT_SETTINGS_CHANGED_INSUFFICIENT (V4L2_EVENT_MSM_VIDC_START + 3)
// The in-tree v4l2-controls.h defines this with a different value; override it.
#undef V4L2_CID_MPEG_MSM_VIDC_BASE
#define V4L2_CID_MPEG_MSM_VIDC_BASE 0x00992000
#undef V4L2_CID_MPEG_VIDC_VIDEO_DPB_COLOR_FORMAT
#undef V4L2_CID_MPEG_VIDC_VIDEO_STREAM_OUTPUT_MODE
#define V4L2_CID_MPEG_VIDC_VIDEO_DPB_COLOR_FORMAT (V4L2_CID_MPEG_MSM_VIDC_BASE + 44)
#define V4L2_CID_MPEG_VIDC_VIDEO_STREAM_OUTPUT_MODE (V4L2_CID_MPEG_MSM_VIDC_BASE + 22)
#define V4L2_QCOM_CMD_FLUSH_CAPTURE (1 << 1)
#define V4L2_QCOM_CMD_FLUSH (4)
#ifndef V4L2_QCOM_BUF_FLAG_EOS
#define V4L2_QCOM_BUF_FLAG_EOS 0x02000000
#endif

#define VIDEO_DEVICE "/dev/video32"
#define OUTPUT_BUFFER_COUNT 8
#define CAPTURE_BUFFER_COUNT 16
#define FPS 20

static void log_err(const char* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  vfprintf(stderr, fmt, args);
  va_end(args);
  fprintf(stderr, "\n");
  fflush(stderr);
}

static bool debug_enabled() {
  static bool d = getenv("HWDEC_DEBUG") != nullptr;
  return d;
}
#define DBG(...) do { if (debug_enabled()) log_err(__VA_ARGS__); } while (0)

static int xioctl(int fd, unsigned long req, void* arg) {
  int r;
  int tries = 0;
  do {
    r = ioctl(fd, req, arg);
  } while (r == -1 && errno == EINTR && tries++ < 100);
  if (r == -1) log_err("ioctl 0x%lx failed: %s", req, strerror(errno));
  return r;
}

static int ion_fd() {
  static int fh = -1;
  if (fh < 0) {
    fh = open("/dev/ion", O_RDWR | O_NONBLOCK);
    if (fh < 0) log_err("failed to open /dev/ion: %s", strerror(errno));
  }
  return fh;
}

struct IONBuf {
  size_t len = 0;
  size_t mmap_len = 0;
  void* addr = nullptr;
  int fd = 0;
  int handle = 0;

  size_t width = 0, height = 0, stride = 0, uv_offset = 0;
  uint8_t* y = nullptr;
  uint8_t* uv = nullptr;

  bool allocate(size_t length) {
    struct ion_allocation_data ion_alloc = {0};
    ion_alloc.len = length + sizeof(uint64_t);
    ion_alloc.align = 4096;
    ion_alloc.heap_id_mask = 1 << ION_IOMMU_HEAP_ID;
    ion_alloc.flags = ION_FLAG_CACHED;

    if (xioctl(ion_fd(), ION_IOC_ALLOC, &ion_alloc) != 0) return false;

    struct ion_fd_data ion_fd_data = {0};
    ion_fd_data.handle = ion_alloc.handle;
    if (xioctl(ion_fd(), ION_IOC_SHARE, &ion_fd_data) != 0) return false;

    void* mmap_addr = mmap(NULL, ion_alloc.len, PROT_READ | PROT_WRITE, MAP_SHARED, ion_fd_data.fd, 0);
    if (mmap_addr == MAP_FAILED) {
      log_err("mmap failed: %s", strerror(errno));
      return false;
    }
    memset(mmap_addr, 0, ion_alloc.len);

    len = length;
    mmap_len = ion_alloc.len;
    addr = mmap_addr;
    handle = ion_alloc.handle;
    fd = ion_fd_data.fd;
    return true;
  }

  void init_yuv(size_t w, size_t h, size_t s, size_t uvoff) {
    width = w;
    height = h;
    stride = s;
    uv_offset = uvoff;
    y = (uint8_t*)addr;
    uv = y + uv_offset;
  }

  void free_buf() {
    if (addr) munmap(addr, mmap_len);
    if (fd > 0) close(fd);
    if (handle > 0) {
      struct ion_handle_data handle_data = {.handle = handle};
      xioctl(ion_fd(), ION_IOC_FREE, &handle_data);
    }
    addr = nullptr;
    fd = 0;
    handle = 0;
  }
};

class MsmVidc {
 public:
  using PacketPtr = std::shared_ptr<std::vector<uint8_t>>;

  MsmVidc() = default;
  ~MsmVidc() {
    shutdown();
    if (fd > 0) close(fd);
  }

  bool init(const char* dev, size_t width, size_t height, uint32_t codec) {
    w = width;
    h = height;
    orig_w = width;
    orig_h = height;
    fd = open(dev, O_RDWR | O_NONBLOCK, 0);
    if (fd < 0) {
      log_err("failed to open %s: %s", dev, strerror(errno));
      return false;
    }
    subscribeEvents();

    v4l2_buf_type out_type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
    if (!setPlaneFormat(out_type, codec)) return false;
    setFPS(FPS);
    request_buffers(out_type, OUTPUT_BUFFER_COUNT);
    if (xioctl(fd, VIDIOC_STREAMON, &out_type) != 0) return false;

    size_t tight = (size_t)orig_w * orig_h * 3 / 2;
    if (!tight_frame.allocate(tight)) return false;
    tight_frame.init_yuv(orig_w, orig_h, orig_w, (size_t)orig_w * orig_h);

    if (!restartCapture()) return false;
    pfd = {fd, POLLIN | POLLOUT | POLLWRNORM | POLLRDNORM | POLLPRI, 0};
    initialized = true;
    return true;
  }

  int run() {
    std::thread reader([this] {
      while (true) {
        uint32_t len = 0;
        if (fread(&len, 4, 1, stdin) != 1) break;
        if (len == 0) {
          abort_requested = true;
          break;
        }
        auto pkt = std::make_shared<std::vector<uint8_t>>(len);
        if (fread(pkt->data(), 1, len, stdin) != len) break;
        {
          std::unique_lock<std::mutex> lk(q_mutex);
          q_cv.wait(lk, [this] { return queue.size() < MAX_QUEUE; });
          queue.push_back(std::move(pkt));
        }
      }
      {
        std::lock_guard<std::mutex> lk(q_mutex);
        input_done = true;
      }
      q_cv.notify_all();
    });

    bool eos_sent = false;
    double eos_deadline = 0;
    int rc = 0;

    while (!abort_requested) {
      feedQueued();

      int timeout = input_done ? 30 : 15;
      int r = pump(timeout);
      if (r < 0) {
        rc = 1;
        break;
      }
      if (r == 1) {
        emitFrame();
        releaseFrame();
      }

      bool qempty;
      {
        std::lock_guard<std::mutex> lk(q_mutex);
        qempty = queue.empty();
      }
      if (input_done && qempty) {
        if (!eos_sent) {
          sendEOS();
          eos_sent = true;
          eos_deadline = nowSeconds() + 2.0;
        } else if (nowSeconds() > eos_deadline) {
          break;
        }
      }
    }

    reader.join();
    shutdown();
    DBG("hwdec wrote %d frames", frames_out);
    return rc;
  }

 private:
  static double nowSeconds() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
  }

  void feedQueued() {
    while (true) {
      int idx = getBufferUnlocked();
      if (idx < 0) return;
      PacketPtr pkt;
      {
        std::lock_guard<std::mutex> lk(q_mutex);
        if (queue.empty()) return;
        pkt = std::move(queue.front());
        queue.pop_front();
      }
      q_cv.notify_one();
      if (!resume_packet) resume_packet = pkt;
      if (pkt->size() > (size_t)out_buf_size) {
        log_err("access unit too large: %zu > %d", pkt->size(), out_buf_size);
        continue;
      }
      memcpy(out_bufs[idx].addr, pkt->data(), pkt->size());
      queueOutputBuffer(idx, pkt->size());
    }
  }

  // Returns -1 error, 0 no new frame, 1 frame ready in cap_bufs[cap_index].
  int pump(int timeout_ms) {
    int rc = poll(&pfd, 1, timeout_ms);
    if (rc < 0) {
      if (errno == EINTR) return 0;
      log_err("poll failed: %s", strerror(errno));
      return -1;
    }
    if (rc == 0) return 0;

    int result;
    while ((result = handleEvent()) > 0) {}
    if (result < 0) return -1;
    while ((result = handleOutput()) > 0) {}
    if (result < 0) return -1;
    return handleCapture();
  }

  int handleCapture() {
    struct v4l2_buffer buf = {0};
    struct v4l2_plane planes[1] = {0};
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    buf.memory = V4L2_MEMORY_USERPTR;
    buf.m.planes = planes;
    buf.length = 1;
    int err = ioctl(fd, VIDIOC_DQBUF, &buf);
    if (err < 0 && errno == EAGAIN) return 0;
    if (err < 0) {
      log_err("VIDIOC_DQBUF CAPTURE failed: %s", strerror(errno));
      return -1;
    }

    const bool has_payload = buf.m.planes[0].bytesused != 0;
    const bool eos = (buf.flags & V4L2_QCOM_BUF_FLAG_EOS) != 0;

    if (!reconfigure_pending && has_payload) {
      cap_index = buf.index;
      return 1;
    }
    if (!reconfigure_pending && !eos) {
      queueCaptureBuffer(buf.index);
    }
    return 0;
  }

  void releaseFrame() {
    queueCaptureBuffer(cap_index);
  }

  void emitFrame() {
    IONBuf& src = cap_bufs[cap_index];
    const size_t row = src.width < src.stride ? src.width : src.stride;
    const size_t copy_w = row < tight_frame.width ? row : tight_frame.width;
    for (size_t r = 0; r < tight_frame.height; r++) {
      memcpy(tight_frame.y + r * tight_frame.width, src.y + r * src.stride, copy_w);
    }
    for (size_t r = 0; r < tight_frame.height / 2; r++) {
      memcpy(tight_frame.uv + r * tight_frame.width, src.uv + r * src.stride, copy_w);
    }

    uint32_t fw = (uint32_t)tight_frame.width;
    uint32_t fh = (uint32_t)tight_frame.height;
    uint32_t flen = (uint32_t)tight_frame.len;
    fwrite(&fw, 4, 1, stdout);
    fwrite(&fh, 4, 1, stdout);
    fwrite(&flen, 4, 1, stdout);
    fwrite(tight_frame.y, 1, flen, stdout);
    fflush(stdout);
    frames_out++;
  }

  bool subscribeEvents() {
    const uint32_t subscriptions[2] = {
      V4L2_EVENT_MSM_VIDC_FLUSH_DONE,
      V4L2_EVENT_MSM_VIDC_PORT_SETTINGS_CHANGED_INSUFFICIENT,
    };
    for (uint32_t event : subscriptions) {
      struct v4l2_event_subscription sub = {.type = event};
      if (xioctl(fd, VIDIOC_SUBSCRIBE_EVENT, &sub) != 0) return false;
    }
    return true;
  }

  void request_buffers(v4l2_buf_type buf_type, unsigned int count) {
    struct v4l2_requestbuffers reqbuf = {0};
    reqbuf.count = count;
    reqbuf.type = buf_type;
    reqbuf.memory = V4L2_MEMORY_USERPTR;
    xioctl(fd, VIDIOC_REQBUFS, &reqbuf);
  }

  bool setPlaneFormat(enum v4l2_buf_type type, uint32_t fourcc) {
    struct v4l2_format fmt = {0};
    fmt.type = type;
    struct v4l2_pix_format_mplane* pix = &fmt.fmt.pix_mp;
    pix->width = (uint32_t)w;
    pix->height = (uint32_t)h;
    pix->pixelformat = fourcc;
    if (xioctl(fd, VIDIOC_S_FMT, &fmt) != 0) return false;

    if (type == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE) {
      out_buf_size = pix->plane_fmt[0].sizeimage;
      DBG("OUTPUT fmt %ux%u sizeimage=%d", pix->width, pix->height, out_buf_size);
      for (int i = 0; i < OUTPUT_BUFFER_COUNT; i++) {
        if (!out_bufs[i].allocate(out_buf_size)) return false;
        out_buf_flag[i] = false;
      }
    } else if (type == V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE) {
      request_buffers(type, CAPTURE_BUFFER_COUNT);
      if (xioctl(fd, VIDIOC_G_FMT, &fmt) != 0) return false;
      const uint32_t y_size = pix->plane_fmt[0].sizeimage;
      const uint32_t y_stride = pix->plane_fmt[0].bytesperline;
      DBG("CAPTURE fmt %ux%u sizeimage=%u bpl=%u", pix->width, pix->height, y_size, y_stride);
      for (int i = 0; i < CAPTURE_BUFFER_COUNT; i++) {
        size_t uv_offset = (size_t)y_stride * pix->height;
        size_t required = uv_offset + (y_stride * pix->height / 2);
        size_t alloc_size = y_size > required ? y_size : required;
        if (!cap_bufs[i].allocate(alloc_size)) return false;
        cap_bufs[i].init_yuv(pix->width, pix->height, y_stride, uv_offset);
      }
    }
    return true;
  }

  bool setFPS(uint32_t fps) {
    struct v4l2_streamparm streamparam = {0};
    streamparam.type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
    streamparam.parm.output.timeperframe.numerator = 1;
    streamparam.parm.output.timeperframe.denominator = fps;
    return xioctl(fd, VIDIOC_S_PARM, &streamparam) == 0;
  }

  bool restartCapture() {
    enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    if (initialized) {
      xioctl(fd, VIDIOC_STREAMOFF, &type);
      struct v4l2_requestbuffers reqbuf = {0};
      reqbuf.type = type;
      reqbuf.memory = V4L2_MEMORY_USERPTR;
      xioctl(fd, VIDIOC_REQBUFS, &reqbuf);
      for (int i = 0; i < CAPTURE_BUFFER_COUNT; ++i) cap_bufs[i].free_buf();
    }
    setDBP();
    if (!setPlaneFormat(type, V4L2_PIX_FMT_NV12)) return false;
    if (xioctl(fd, VIDIOC_STREAMON, &type) != 0) return false;
    for (int i = 0; i < CAPTURE_BUFFER_COUNT; ++i) queueCaptureBuffer(i);
    return true;
  }

  bool queueCaptureBuffer(int i) {
    struct v4l2_buffer buf = {0};
    struct v4l2_plane planes[1] = {0};
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    buf.memory = V4L2_MEMORY_USERPTR;
    buf.index = i;
    buf.m.planes = planes;
    buf.length = 1;
    planes[0].m.userptr = (unsigned long)cap_bufs[i].addr;
    planes[0].length = cap_bufs[i].len;
    planes[0].reserved[0] = cap_bufs[i].fd;
    planes[0].reserved[1] = 0;
    planes[0].bytesused = cap_bufs[i].len;
    planes[0].data_offset = 0;
    return xioctl(fd, VIDIOC_QBUF, &buf) == 0;
  }

  bool queueOutputBuffer(int i, size_t size) {
    struct v4l2_buffer buf = {0};
    struct v4l2_plane planes[1] = {0};
    buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
    buf.memory = V4L2_MEMORY_USERPTR;
    buf.index = i;
    buf.flags = V4L2_BUF_FLAG_TIMESTAMP_COPY;
    buf.m.planes = planes;
    buf.length = 1;
    planes[0].m.userptr = (unsigned long)out_bufs[i].addr;
    planes[0].length = out_buf_size;
    planes[0].reserved[0] = out_bufs[i].fd;
    planes[0].reserved[1] = 0;
    planes[0].bytesused = size;
    planes[0].data_offset = 0;
    if (xioctl(fd, VIDIOC_QBUF, &buf) != 0) return false;
    out_buf_flag[i] = true;
    return true;
  }

  bool setDBP() {
    struct v4l2_ext_control control[2] = {0};
    struct v4l2_ext_controls controls = {0};
    control[0].id = V4L2_CID_MPEG_VIDC_VIDEO_STREAM_OUTPUT_MODE;
    control[0].value = 1;
    control[1].id = V4L2_CID_MPEG_VIDC_VIDEO_DPB_COLOR_FORMAT;
    control[1].value = 0;
    controls.count = 2;
    controls.ctrl_class = V4L2_CTRL_CLASS_MPEG;
    controls.controls = control;
    return xioctl(fd, VIDIOC_S_EXT_CTRLS, &controls) == 0;
  }

  int getBufferUnlocked() {
    for (int i = 0; i < OUTPUT_BUFFER_COUNT; i++) {
      if (!out_buf_flag[i]) return i;
    }
    return -1;
  }

  int handleOutput() {
    struct v4l2_buffer buf = {0};
    struct v4l2_plane planes[1] = {0};
    buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
    buf.memory = V4L2_MEMORY_USERPTR;
    buf.m.planes = planes;
    buf.length = 1;
    int err = ioctl(fd, VIDIOC_DQBUF, &buf);
    if (err < 0 && errno == EAGAIN) return 0;
    if (err < 0) {
      log_err("VIDIOC_DQBUF OUTPUT failed: %s", strerror(errno));
      return -1;
    }
    out_buf_flag[buf.index] = false;
    return 1;
  }

  int handleEvent() {
    struct v4l2_event event = {0};
    int err = ioctl(fd, VIDIOC_DQEVENT, &event);
    if (err < 0 && (errno == EAGAIN || errno == ENOENT)) return 0;
    if (err < 0) {
      log_err("VIDIOC_DQEVENT failed: %s", strerror(errno));
      return -1;
    }
    switch (event.type) {
      case V4L2_EVENT_MSM_VIDC_PORT_SETTINGS_CHANGED_INSUFFICIENT: {
        unsigned int* ptr = (unsigned int*)event.u.data;
        w = ptr[1];
        h = ptr[0];
        DBG("reconfig -> %zux%zu", w, h);
        struct v4l2_decoder_cmd dec = {0};
        dec.flags = V4L2_QCOM_CMD_FLUSH_CAPTURE;
        dec.cmd = V4L2_QCOM_CMD_FLUSH;
        xioctl(fd, VIDIOC_DECODER_CMD, &dec);
        reconfigure_pending = true;
        break;
      }
      case V4L2_EVENT_MSM_VIDC_FLUSH_DONE: {
        unsigned int* ptr = (unsigned int*)event.u.data;
        unsigned int flags = ptr[0];
        if ((flags & V4L2_QCOM_CMD_FLUSH_CAPTURE) && reconfigure_pending) {
          restartCapture();
          reconfigure_pending = false;
        }
        break;
      }
      default:
        break;
    }
    return 1;
  }

  void sendEOS() {
    struct v4l2_decoder_cmd command = {0};
    command.cmd = V4L2_DEC_CMD_STOP;
    xioctl(fd, VIDIOC_DECODER_CMD, &command);
  }

  // Stop both queues and release the ION buffers so the msm_vidc driver frees
  // its internal allocations. Without this, repeated decoder sessions leak
  // driver memory and later inits fail with ENOMEM.
  void shutdown() {
    if (fd <= 0 || torn_down) return;
    torn_down = true;
    v4l2_buf_type out = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
    v4l2_buf_type cap = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    xioctl(fd, VIDIOC_STREAMOFF, &cap);
    xioctl(fd, VIDIOC_STREAMOFF, &out);
    for (int i = 0; i < CAPTURE_BUFFER_COUNT; i++) cap_bufs[i].free_buf();
    for (int i = 0; i < OUTPUT_BUFFER_COUNT; i++) out_bufs[i].free_buf();
    tight_frame.free_buf();
  }

  int fd = 0;
  bool initialized = false;
  bool reconfigure_pending = false;
  bool torn_down = false;
  std::atomic<bool> abort_requested{false};

  size_t w = 1928, h = 1208;
  size_t orig_w = 1928, orig_h = 1208;

  int out_buf_size = 0;
  int cap_index = 0;
  int frames_out = 0;

  IONBuf out_bufs[OUTPUT_BUFFER_COUNT];
  IONBuf cap_bufs[CAPTURE_BUFFER_COUNT];
  IONBuf tight_frame;

  bool out_buf_flag[OUTPUT_BUFFER_COUNT] = {false};

  struct pollfd pfd = {};

  std::deque<PacketPtr> queue;
  std::mutex q_mutex;
  std::condition_variable q_cv;
  static constexpr size_t MAX_QUEUE = 16;
  bool input_done = false;
  PacketPtr resume_packet;
};

int main(int argc, char** argv) {
  signal(SIGPIPE, SIG_IGN);

  size_t width = argc > 1 ? (size_t)atoi(argv[1]) : 1928;
  size_t height = argc > 2 ? (size_t)atoi(argv[2]) : 1208;

  MsmVidc vidc;
  if (!vidc.init(VIDEO_DEVICE, width, height, V4L2_PIX_FMT_HEVC)) {
    log_err("failed to init decoder");
    uint32_t zero[3] = {0, 0, 0};
    fwrite(zero, 4, 3, stdout);
    fflush(stdout);
    return 1;
  }
  log_err("hwdec ready %zux%zu", width, height);
  return vidc.run();
}
