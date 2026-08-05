# 运行时公开 API

## 1. 本讲目标

Vortex 的主机程序（host program）并不直接「踩」硬件寄存器，而是通过一套 C 语言运行时 API 与设备打交道。本讲聚焦于这套 API 中最易上手的入口——同步 API `vortex.h`。

学完本讲后，读者应该能够：

1. 说清 `vortex.h`（同步）与 `vortex2.h`（异步）两层 API 的关系，理解「同步 API 是异步 API 的薄封装」这句话。
2. 独立使用 `vx_dev_open` / `vx_dev_close`、`vx_mem_alloc`、`vx_start`、`vx_ready_wait` 这组核心调用。
3. 理解 `vx_upload_kernel_file`（把 `.vxbin` 装进设备内存）和 `vx_mpm_query`（读性能计数器）各自的作用。
4. 读懂一份典型的 Vortex 主机程序的控制流：打开设备 → 分配缓冲 → 拷入数据 → 上传内核 → 启动 → 等待 → 拷回结果 → 关闭。

本讲只讲「主机侧怎么调用」，不展开设备内部（命令处理器、KMU、页表）的实现——那是后续 u3-l2 ~ u3-l4 的内容。

## 2. 前置知识

在开始前，建议你先建立以下几个心智模型（u1 系列与 u2 系列已讲过，这里只做一句话回顾）：

- **主机与设备分离**：Vortex 是一块 GPU，主机（CPU）程序通过运行时库 `libvortex.so` 向设备下发任务，任务包括「拷贝数据」「启动内核（kernel）」「等待完成」。
- **后端可切换**：`libvortex.so` 是一个 stub（桩），按环境变量 `$VORTEX_DRIVER` 在 simx（C++ 仿真）、rtlsim、opae（Intel FPGA）、xrt（Xilinx FPGA）等后端间切换。主机代码不变，后端变。
- **内核是 `.vxbin` 文件**：设备上跑的「程序」是一个编译好的 RISC-V 二进制 `.vxbin`，需要先上传到设备内存，再启动。
- **句柄（handle）**：主机用一个不透明的指针（如 `vx_device_h`）代表「设备」，用 `vx_buffer_h` 代表「设备上的一段内存」。你只拿句柄，不直接碰地址。

如果这些概念你还陌生，建议先回头读 u1-l1（全栈总览）和 u1-l4（首次运行）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [`sw/runtime/include/vortex.h`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex.h) | **同步 API 头文件**，本讲主角。声明了 `vx_dev_open`、`vx_mem_alloc`、`vx_start` 等全部同步入口。 |
| [`sw/runtime/include/vortex2.h`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex2.h) | **异步 API 头文件**，规范（canonical）API。定义了句柄类型、`VX_CAPS_*` 能力 ID、`VX_MEM_*` 内存标志、`vx_result_t` 错误码等。`vortex.h` 直接 `#include` 它。 |
| [`sw/runtime/common/legacy_runtime.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp) | `vortex.h` 中设备/内存/启动/同步等函数的实现：每个同步函数都是对 `vortex2.h` 异步入口的薄封装。 |
| [`sw/runtime/common/legacy_utils.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_utils.cpp) | `vx_upload_kernel_file`、`vx_upload_bytes`、`vx_mpm_query`、`vx_dump_perf` 等工具函数的实现。 |
| [`docs/designs/vortex_runtime_api.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortex_runtime_api.md) | 运行时 API 的设计哲学文档，讲「形状锁定（shape-lock）」与「累加式演进」原则。 |
| [`tests/regression/vecadd_v1/main.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/vecadd_v1/main.cpp) | **用同步 API 写的向量加法示例**，本讲反复引用的控制流样板。 |

> 提示：另一个常见示例 `tests/regression/demo` 用的是**异步** `vortex2.h` API，不是本讲的同步 API。两者的对照会在 4.1 讲到。

---

## 4. 核心概念与源码讲解

### 4.1 两层 API：同步封装与异步核心

#### 4.1.1 概念说明

Vortex 的运行时 API 分两层：

- **`vortex2.h`——异步核心（canonical API）**：这是一套 Vulkan/CUDA 风格的最小化核心，提供「设备 / 队列 / 缓冲 / 事件 / 模块 / 内核」六类不透明句柄，所有命令都是**异步提交**（submit）的——提交后立刻返回，完成时机由「事件（event）」跟踪。上层翻译器（PoCL 对接 OpenCL、chipStar 对接 HIP、vortexpipe 对接 Vulkan）都直接瞄准这一层。
- **`vortex.h`——同步封装（thin wrapper）**：在 `vortex2.h` 之上盖了一层「调一次就阻塞到完成」的同步入口。函数更少、心智负担更低，是写简单主机程序和大多数回归测试的首选。

两者的设计原则写在 `vortex_runtime_api.md` 里：**累加式（additive）而非破坏式（shape-breaking）**——v1 的函数形状（参数、返回值）一旦锁定就不再改，新能力只能以「不改变现有形状」的方式追加。这正是 `vortex.h` 能稳定地当一层薄封装的前提。

一句话总结：**主机程序面对的是 `vortex.h`（简单、同步），而 `vortex.h` 把每个调用翻译成 `vortex2.h` 的「提交 + 等待」两步，再由 `vortex2.h` 分发到具体后端。**

#### 4.1.2 核心流程

调用链可以画成三层：

```
主机程序 (main.cpp)
    │  调用 vx_dev_open / vx_mem_alloc / vx_start / ...  （同步，返回 int）
    ▼
vortex.h  (legacy_runtime.cpp / legacy_utils.cpp)
    │  每个同步函数 = 一次 vortex2.h 异步提交 + 一次事件等待
    ▼
vortex2.h (vortex2_internal.h + 后端 callbacks)
    │  经 stub 按 $VORTEX_DRIVER 分发
    ▼
后端实现：simx / rtlsim / opae / xrt / gem5
```

同步封装的关键技巧是「**提交后立刻等待**」。`legacy_runtime.cpp` 里有一个统一的辅助模板 `enqueue_and_wait`：把一个异步操作提交到设备的默认队列，拿到事件，再原地等到它完成、释放事件。于是对调用者来说，这一步看起来就是「同步」的。

返回值约定也不同：同步 API 返回 `int`，成功是 `0`、失败是 `-1`；异步 API 返回 `vx_result_t` 枚举（`VX_SUCCESS=0`，其余为各类错误码）。封装层用一个 `to_int()` 把后者压成前者。

#### 4.1.3 源码精读

`vortex.h` 开头的注释直接点明了它的定位——「`vortex2.h` 之上的薄封装」，并且把句柄、能力 ID、ISA 标志、内存标志都委托给 `vortex2.h` 定义：

[sw/runtime/include/vortex.h:17-21](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex.h#L17-L21)

```c
// Synchronous Vortex runtime API — a thin wrapper over vortex2.h.
// Shared handles (vx_device_h, vx_buffer_h), device-capability IDs
// (VX_CAPS_*), ISA flags (VX_ISA_*) and memory-access flags (VX_MEM_*)
// are defined in vortex2.h.
#include <vortex2.h>
```

也就是说，`vortex.h` 自己几乎不定义类型，只声明函数；要理解句柄和标志，得去 `vortex2.h` 看。

封装层的两个关键工具在实现文件里。第一个是错误码压平：

[sw/runtime/common/legacy_runtime.cpp:23-25](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L23-L25)

```cpp
inline int to_int(vx_result_t r) {
    return (r == VX_SUCCESS) ? 0 : -1;
}
```

第二个是「提交 + 等待」模板，它揭示了同步 API 的本质——把异步操作丢进设备的「legacy 默认队列」，然后原地等事件完成：

[sw/runtime/common/legacy_runtime.cpp:29-41](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L29-L41)

```cpp
template <typename Fn>
vx_result_t enqueue_and_wait(Device* dev, Fn&& fn) {
    Queue* q = dev->legacy_default_queue();
    if (!q) return VX_ERR_OUT_OF_HOST_MEMORY;
    vx_event_h ev = nullptr;
    auto r = fn(to_handle(q), &ev);          // ① 提交异步操作，拿事件
    if (r != VX_SUCCESS) return r;
    if (ev) {
        r = vx_event_wait_value(ev, 1, VX_TIMEOUT_INFINITE);  // ② 原地等待
        vx_event_release(ev);                                  // ③ 释放事件
    }
    return r;
}
```

设计文档 `vortex_runtime_api.md` 把这种「先把复杂度推给上层翻译器和逐块辅助函数、核心只保留最小形状」的哲学讲得很清楚：

[docs/designs/vortex_runtime_api.md:17-24](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortex_runtime_api.md#L17-L24)

> `vortex2.h` is a minimal, Vulkan/CUDA-style core … with complexity pushed to upper-layer translators … The governing rule is **additive vs. shape-breaking**: the v1 surface is locked …

#### 4.1.4 代码实践

**实践目标**：亲眼看到「同一件事，两套 API 写法不同」。

**操作步骤**：

1. 打开 [tests/regression/vecadd_v1/main.cpp:1-6](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/vecadd_v1/main.cpp#L1-L6)，注意它 `#include <vortex.h>`（同步）。
2. 打开 [tests/regression/demo/main.cpp:1-6](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L1-L6)，注意它 `#include <vortex2.h>`（异步）。
3. 在两个文件里分别搜索「打开设备」这一步：
   - vecadd_v1 用 `vx_dev_open(&device)`（[第 129 行](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/vecadd_v1/main.cpp#L129)）；
   - demo 用 `vx_device_open(0, &device)`（[第 139 行](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L139)），随后还要 `vx_queue_create` 建队列。

**需要观察的现象**：vecadd_v1 没有「队列」概念，因为它由同步封装在内部偷偷用了一个默认队列；demo 显式管理队列和事件。

**预期结果**：你能用一句话讲出「同步 API 省掉了队列/事件，代价是每次调用都阻塞」。

> 本实践为源码阅读型，无需运行；若要实际跑，参见 4.3.4 或第 5 节。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `vortex.h` 要把句柄类型（`vx_device_h` 等）放在 `vortex2.h` 里定义，而不是自己定义一份？

> **答案**：因为同步与异步两层共享同一套句柄（`vx_device_h` 就是 `void*`）。让 `vortex2.h` 唯一定义、`vortex.h` 直接 `#include`，可以避免两套定义漂移，也保证两套 API 能混用同一个设备对象。

**练习 2**：同步 API 返回 `int`，异步 API 返回 `vx_result_t`。封装层用什么把它们对接起来的？这种做法丢失了什么信息？

> **答案**：用 `to_int(vx_result_t)` 把 `VX_SUCCESS` 映射成 `0`、其余一律映射成 `-1`。代价是丢失了「具体是哪一种错误」（如 `VX_ERR_OUT_OF_DEVICE_MEMORY` vs `VX_ERR_TIMEOUT`），调用者只知道「失败了」。需要精细错误信息时应直接用异步 API。

---

### 4.2 设备生命周期与内存缓冲管理

#### 4.2.1 概念说明

这是 `vortex.h` 里函数最多的一块，对应 GPU 编程里最基础的两件事：**拿到设备**、**在设备上分配/读写内存**。

- **设备句柄 `vx_device_h`**：一个不透明指针，代表「连上的那块 Vortex 设备」。所有操作都围绕它进行。
- **缓冲句柄 `vx_buffer_h`**：代表设备上一段连续内存。注意它**不是地址**，而是一个对象——里面除了起始地址，还记着大小、权限标志、所属设备等元数据。
- **能力查询（capability query）**：主机在启动内核前，往往要先问设备「你有几个核、每核几 warp、每 warp 几线程」，才能算出合适的启动维度。这就是 `vx_dev_caps`（同步）/ `vx_device_query`（异步）的作用。
- **内存访问标志 `VX_MEM_*`**：声明一段内存的读写权限（只读 / 只写 / 读写），以及是否钉住（pin）、是否物理地址等。

这套接口刻意对齐了 CUDA / OpenCL 的主机接口风格：`vx_mem_alloc` ≈ `clCreateBuffer` / `cudaMalloc`，`vx_copy_to_dev` ≈ `clEnqueueWriteBuffer` / `cudaMemcpy`（H2D）。

#### 4.2.2 核心流程

一个最小而完整的设备生命周期：

```
1. vx_dev_open(&device)            // 打开设备，拿到句柄
2. vx_dev_caps(device, VX_CAPS_*, &v)   // (可选) 查询能力
3. vx_mem_alloc(device, size, flags, &buf)  // 分配设备内存
   vx_mem_address(buf, &dev_addr)        // 拿到设备侧地址（传给内核用）
4. vx_copy_to_dev(buf, host_ptr, 0, size)   // 主机 → 设备
   ...... 启动并等待内核（见 4.3）......
   vx_copy_from_dev(host_ptr, buf, 0, size) // 设备 → 主机
5. vx_mem_free(buf)                // 释放缓冲
6. vx_dev_close(device)            // 关闭设备
```

关于启动维度的计算，demo 里有一段很典型的算术（本讲同步 API `vx_check_occupancy` 内部也用同样的逻辑）。设备每核可容纳的线程数为：

\[
\text{threads\_per\_core} = \text{num\_warps} \times \text{num\_threads}
\]

主机据此决定一个 block 放多少线程、一共放多少 block。这部分会在 u4（设备内核 API）深入，本讲只需知道「能力查询的返回值会喂给启动维度计算」。

#### 4.2.3 源码精读

**句柄类型**全部定义在 `vortex2.h`，且刻意用 `void*` 以保证 ABI 稳定：

[sw/runtime/include/vortex2.h:46-53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex2.h#L46-L53)

```c
typedef void* vx_device_h;
typedef void* vx_buffer_h;

typedef struct vx_queue*  vx_queue_h;
typedef struct vx_event*  vx_event_h;
typedef struct vx_module* vx_module_h;
typedef struct vx_kernel* vx_kernel_h;
```

**能力 ID** 是一组固定编号，`vx_dev_caps` / `vx_device_query` 的第二个参数就取这些值。注意它们都是「设备能力」而非「构建配置」——头文件注释特别强调这个公共头**自包含、不依赖构建期的 `VX_config.h`**，配置在运行时通过查询获得：

[sw/runtime/include/vortex2.h:59-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex2.h#L59-L74)

```c
#define VX_CAPS_NUM_THREADS         0x1   // 每个 warp 的线程数
#define VX_CAPS_NUM_WARPS           0x2   // 每个核的 warp 数
#define VX_CAPS_NUM_CORES           0x3   // 总核数
#define VX_CAPS_GLOBAL_MEM_SIZE     0x5   // 全局内存大小（字节）
#define VX_CAPS_LOCAL_MEM_SIZE      0x6   // 每核本地内存大小
#define VX_CAPS_ISA_FLAGS           0x7   // 设备 ISA 标志
...
```

> 这条「运行时查询而非编译期宏」的设计，正是 u2-l3 讲的「软硬件边界隔离」在 API 层的体现：公共头不 include 私有配置头。

**内存访问标志**：

[sw/runtime/include/vortex2.h:117-128](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex2.h#L117-L128)

```c
#define VX_MEM_READ                 0x1
#define VX_MEM_WRITE                0x2
#define VX_MEM_READ_WRITE           0x3
#define VX_MEM_PIN_MEMORY           0x4
#define VX_MEM_PHYS                 0x8   // 返回物理地址（不做虚地址翻译）
#define VX_MEM_HOST                 0x10  // 分配在主机内存（CP 的 host 通道用）
```

现在看同步 API 的声明与实现。**打开/关闭设备**：

[sw/runtime/include/vortex.h:31-34](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex.h#L31-L34)

```c
int vx_dev_open(vx_device_h* hdevice);   // 打开并连接设备
int vx_dev_close(vx_device_h hdevice);   // 所有操作完成后关闭
```

其实现里，`vx_dev_open` 直接转发给异步 `vx_device_open(0, ...)`（固定打开第 0 号设备）：

[sw/runtime/common/legacy_runtime.cpp:49-52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L49-L52)

```cpp
extern "C" int vx_dev_open(vx_device_h* hdevice) {
    if (!hdevice) return -1;
    return to_int(vx_device_open(0, hdevice));
}
```

`vx_dev_close` 多做一步：先排空（drain）任何还在飞的 legacy 启动，避免工作线程比设备活得还久，再释放设备：

[sw/runtime/common/legacy_runtime.cpp:54-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L54-L64)

**分配内存与查询地址**：同步 `vx_mem_alloc` 转发异步 `vx_buffer_create`：

[sw/runtime/common/legacy_runtime.cpp:82-85](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L82-L85)

```cpp
extern "C" int vx_mem_alloc(vx_device_h hdevice, uint64_t size, int flags,
                            vx_buffer_h* hbuffer) {
    return to_int(vx_buffer_create(hdevice, size, (uint32_t)flags, hbuffer));
}
```

**主机↔设备拷贝**：`vx_copy_to_dev` 是 `enqueue_and_wait` 的典型用例——提交一次异步写（`vx_enqueue_write`），原地等待完成：

[sw/runtime/common/legacy_runtime.cpp:115-124](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L115-L124)

```cpp
extern "C" int vx_copy_to_dev(vx_buffer_h hbuffer, const void* host_ptr,
                              uint64_t dst_offset, uint64_t size) {
    if (!hbuffer) return -1;
    Buffer* buf = to_buffer(hbuffer);
    return to_int(enqueue_and_wait(buf->device(),
        [&](vx_queue_h q, vx_event_h* ev) {
            return vx_enqueue_write(q, hbuffer, dst_offset, host_ptr, size,
                                    0, nullptr, ev);
        }));
}
```

最后看一个**真实使用**：vecadd_v1 里「分配三个缓冲并取地址」的片段，它把分配到的设备地址存进 `kernel_arg`，待会儿要随内核一起交给设备：

[tests/regression/vecadd_v1/main.cpp:142-147](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/vecadd_v1/main.cpp#L142-L147)

```cpp
RT_CHECK(vx_mem_alloc(device, buf_size, VX_MEM_READ, &src0_buffer));
RT_CHECK(vx_mem_address(src0_buffer, &kernel_arg.src0_addr));
RT_CHECK(vx_mem_alloc(device, buf_size, VX_MEM_READ, &src1_buffer));
RT_CHECK(vx_mem_address(src1_buffer, &kernel_arg.src1_addr));
RT_CHECK(vx_mem_alloc(device, buf_size, VX_MEM_WRITE, &dst_buffer));
RT_CHECK(vx_mem_address(dst_buffer, &kernel_arg.dst_addr));
```

> 注意：源/目的缓冲分别用 `VX_MEM_READ` / `VX_MEM_WRITE` 标记——这只读的输入、只写的输出，语义上对齐 OpenCL 的 `CL_MEM_READ_ONLY` / `CL_MEM_WRITE_ONLY`。

#### 4.2.4 代码实践

**实践目标**：跟踪 vecadd_v1 从打开设备到拷入数据的全过程，画出对象关系。

**操作步骤**：

1. 读 [tests/regression/vecadd_v1/main.cpp:129-170](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/vecadd_v1/main.cpp#L129-L170)。
2. 列出此阶段创建的所有句柄：`device`、`src0_buffer`、`src1_buffer`、`dst_buffer`。
3. 标注每次 `vx_copy_to_dev` 的方向（H2D）和目标缓冲（[第 166、170 行](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/vecadd_v1/main.cpp#L166-L170)）。
4. 画一张关系图：`device` 持有三个 `buffer`，每个 `buffer` 内部含一个设备地址；主机 `h_src0/h_src1` 经 `vx_copy_to_dev` 灌进前两个 `buffer`。

**需要观察的现象**：`vx_mem_address` 取到的地址是 `uint64_t` 设备虚拟地址，被写进 `kernel_arg`——主机从不直接解引用它。

**预期结果**：得到一张「主机缓冲 →（拷贝）→ 设备 buffer →（地址）→ kernel_arg」的对象流向图。

> 运行验证（可选）：在 build 树里执行 `./ci/blackbox.sh --driver=simx --app=vecadd_v1`（若该 app 不在 blackbox 清单，则 `make -C tests/regression/vecadd_v1 run-simx`）。观察标准输出里的 `dev_src0=0x...` 等地址打印。具体能否在你本地跑通「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`vx_mem_alloc` 返回的是地址还是句柄？如果你要在内核里访问这块内存，还需要额外调用哪个函数？

> **答案**：返回的是**句柄** `vx_buffer_h`，不是地址。要在内核里访问，还需调 `vx_mem_address(buf, &addr)` 取出 `uint64_t` 设备地址，再把这个地址作为参数传给内核（如 vecadd_v1 里写进 `kernel_arg.src0_addr`）。

**练习 2**：`VX_MEM_PHYS`（0x8）和 `VX_MEM_HOST`（0x10）分别有什么特殊含义？什么场景下会用到 `VX_MEM_HOST`？

> **答案**：`VX_MEM_PHYS` 让分配直接返回物理设备地址、跳过虚地址翻译；`VX_MEM_HOST` 把缓冲分配在主机内存里（经平台从桥 / Host Memory Access 孔径），让命令处理器的 host-memory 主端口（`m_axi_host`）能访问它，常用于 CP 命令环和主机↔设备 DMA 暂存。simx/rtlsim/gem5 这些没有真实「主机/设备内存分裂」的后端会忽略 `VX_MEM_HOST`。

**练习 3**：为什么 `vx_dev_caps(VX_CAPS_NUM_CORES, ...)` 要在**运行时**查，而不是用一个编译期宏 `NUM_CORES`？

> **答案**：因为同一份编译好的主机程序可能跑在不同配置的设备上（同一个 stub 可对接不同 `VX_config.toml` 构建的后端）。运行时查询保证了「一份主机二进制适配多棵设备树」，也维持了公共头不依赖私有配置头的边界纪律（见 u2-l3）。

---

### 4.3 内核上传、启动同步与性能查询

#### 4.3.1 概念说明

设备有了、内存分好了、数据也拷进去了，接下来是最关键的一步：**把内核送进设备并启动它**。本模块讲三个函数：

- **`vx_upload_kernel_file`**：读取磁盘上的 `.vxbin` 文件，按它**链接时确定的虚拟地址范围（VMA）**把它放到设备内存里。注意它不是随便找个空位放，而是尊重二进制自带的 `min_vma/max_vma`——因为内核里的全局变量、代码地址都是按这个 VMA 编译的。
- **`vx_start`**：启动设备执行。它把「内核入口地址」「参数地址」「grid/block 维度」等一整套描述符写进设备的 **KMU（Kernel Management Unit）配置寄存器（DCR）**，然后触发一次启动。它是**异步**的——提交后立即返回。
- **`vx_ready_wait`**：阻塞等待最近一次 `vx_start` 完成。与 `vx_start` 配对使用。

此外还有性能相关的两个工具函数：

- **`vx_mpm_query`**：读取一个 64 位 MPM（Multi-Performance Monitor）硬件性能计数器。
- **`vx_dump_perf`**：把整份格式化的性能报告打印到流（受环境变量 `VORTEX_PROFILING` 控制）。

#### 4.3.2 核心流程

同步启动的完整流程（vecadd_v1 的写法）：

```
1. vx_upload_kernel_file(device, "kernel.vxbin", &krnl_buf)
       // 读 .vxbin → reserve 它的 VMA 区间 → 拷代码段 → 清零 BSS
2. vx_upload_bytes(device, &kernel_arg, sizeof(kernel_arg), &args_buf)
       // 把「参数结构体」也作为普通字节上传成一块设备内存
3. vx_start(device, krnl_buf, args_buf)
       // ① 取内核入口 PC = krnl_buf 的设备地址
       // ② 取参数指针 = args_buf 的设备地址
       // ③ 查询 num_cores/num_warps/num_threads，算出 grid/block
       // ④ 写一整套 VX_DCR_KMU_* 寄存器（STARTUP_ADDR/ARG、GRID/BLOCK_DIM...）
       // ⑤ 提交一次 launch，把完成事件记在设备里
4. vx_ready_wait(device, VX_MAX_TIMEOUT)
       // 取出上一步记的事件，阻塞等到它完成
```

这里有一个微妙但重要的点：同步 API 把「参数」当成**一块普通设备内存**上传（`vx_upload_bytes`），再把这块内存的地址写进 KMU 的 `STARTUP_ARG` 寄存器；设备端内核则通过 `csr_read(VX_CSR_MSCRATCH)` 读到这块参数。这与异步 API 的「UVA 原生指针参数」（`vx_launch_info_t.args_host`，由运行时自动暂存）是两条不同路径——后者会在 u3-l4 讲。

KMU 的配置寄存器编号定义在 `VX_types.toml`（再由生成器产出 `VX_types.h`），例如网格维度：

[VX_types.toml:73](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L73)

```toml
VX_DCR_KMU_GRID_DIM_X   = 0x019
```

`vx_start` 实现里那一长串 `{ addr, value }` 就是往这些寄存器里写值。

#### 4.3.3 源码精读

**声明**（同步 API 头文件）：

[sw/runtime/include/vortex.h:66-69](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex.h#L66-L69)

```c
int vx_start(vx_device_h hdevice, vx_buffer_h hkernel, vx_buffer_h harguments);
int vx_ready_wait(vx_device_h hdevice, uint64_t timeout);   // 毫秒级超时
```

超时常量 `VX_MAX_TIMEOUT` = 24 小时，是 `vx_ready_wait` 的常用实参：

[sw/runtime/include/vortex.h:27-28](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex.h#L27-L28)

```c
#define VX_MAX_TIMEOUT              (24*60*60*1000)   // 24 Hr
```

**`vx_upload_kernel_file` 的实现**：读文件后委托给 `vx_upload_kernel_bytes`。后者做的事很有教学意义——它把 `.vxbin` 头部的 `min_vma`、`max_vma` 当作两个 64 位整数读出，据此 `vx_mem_reserve` 一段设备地址区间，把代码段拷进去、把 BSS（未初始化全局变量）区清零：

[sw/runtime/common/legacy_utils.cpp:25-45](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_utils.cpp#L25-L45)

```cpp
extern int vx_upload_kernel_bytes(vx_device_h hdevice, const void* content,
                                  uint64_t size, vx_buffer_h* hbuffer) {
  ...
  auto bytes = reinterpret_cast<const uint64_t*>(content);
  auto min_vma = *bytes++;        // 二进制自带的最低虚拟地址
  auto max_vma = *bytes++;        // 最高虚拟地址
  auto bin_size = size - 2 * 8;
  auto runtime_size = (max_vma - min_vma);

  vx_buffer_h _hbuffer;
  CHECK_ERR(vx_mem_reserve(hdevice, min_vma, runtime_size, 0, &_hbuffer), ...);
  // 代码段标只读，全局变量段标读写
  CHECK_ERR(vx_mem_access(_hbuffer, 0, bin_size, VX_MEM_READ), ...);
  CHECK_ERR(vx_mem_access(_hbuffer, bin_size, runtime_size - bin_size,
                          VX_MEM_READ_WRITE), ...);
  CHECK_ERR(vx_copy_to_dev(_hbuffer, bytes, 0, bin_size), ...);  // 拷代码
  ...
}
```

> 这解释了为什么 `vx_upload_kernel_file` 返回的缓冲地址就等于内核入口 PC——代码段被放在了它自己要求的 `min_vma` 处。

**`vx_start` 的实现**是本讲最长的函数，也是「主机如何编程设备」的核心。它先查询设备规模、算出 grid/block；再取出内核 PC 和参数地址；然后把一整套 KMU 寄存器依次写下去；最后提交一次 launch。下面节选「查询规模 + 写 KMU 寄存器 + 提交 launch」三段：

[sw/runtime/common/legacy_runtime.cpp:173-186](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L173-L186)

```cpp
uint64_t num_cores = 0, num_threads = 0, num_warps = 0;
if (vx_device_query(hdevice, VX_CAPS_NUM_CORES,   &num_cores)   != VX_SUCCESS) return -1;
if (vx_device_query(hdevice, VX_CAPS_NUM_THREADS, &num_threads) != VX_SUCCESS) return -1;
if (vx_device_query(hdevice, VX_CAPS_NUM_WARPS,   &num_warps)   != VX_SUCCESS) return -1;
... // prepare_kernel_launch_params 算出 block_size / warp_step
uint32_t full_grid[3]  = {(uint32_t)num_cores, 1, 1};   // grid = 核数
uint32_t full_block[3] = {eff_block_dim[0], 1, 1};
```

[sw/runtime/common/legacy_runtime.cpp:194-211](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L194-L211)

```cpp
uint64_t pc   = kernel->dev_address();   // 内核入口 PC
uint64_t argp = args->dev_address();     // 参数块地址
struct { uint32_t addr; uint32_t value; } kmu_writes[] = {
    { VX_DCR_KMU_STARTUP_ADDR0, (uint32_t)(pc & 0xffffffffu) },
    { VX_DCR_KMU_STARTUP_ADDR1, (uint32_t)(pc >> 32)         },
    { VX_DCR_KMU_STARTUP_ARG0,  (uint32_t)(argp & 0xffffffffu) },
    { VX_DCR_KMU_STARTUP_ARG1,  (uint32_t)(argp >> 32)        },
    { VX_DCR_KMU_BLOCK_DIM_X,   full_block[0] },
    ...
    { VX_DCR_KMU_GRID_DIM_X,    full_grid[0]  },
    ...
};
```

[sw/runtime/common/legacy_runtime.cpp:222-233](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L222-L233)

```cpp
// 所有 KMU DCR 已编程完；enqueue_launch 仅触发 CMD_LAUNCH
vx_launch_info_t li = {};
li.struct_size = sizeof(li);
li.kernel      = nullptr;   // legacy 逃生舱：PC/ARG 已由上面的 DCR 写好
li.args_host   = nullptr;
li.args_size   = 0;
li.ndim        = 0;
vx_event_h ev = nullptr;
auto r = vx_enqueue_launch(to_handle(q), &li, 0, nullptr, &ev);
...
dev->legacy_remember_last_event(to_event(ev));   // 把事件记下，留给 vx_ready_wait
```

注意这里用了 `vortex_runtime_api.md` 里讲的「**legacy 逃生舱**」：`kernel == NULL`、`ndim == 0` 表示「调用者已经自己把 KMU 的 PC/ARG DCR 编程好了，运行时只需触发启动」。异步 API 里 `kernel` 非 NULL 时走的是另一条「运行时自动暂存参数」的路径。

**`vx_ready_wait`**：取出 `vx_start` 记下的那个事件，阻塞等到它完成，毫秒超时换算成纳秒：

[sw/runtime/common/legacy_runtime.cpp:236-247](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L236-L247)

**性能查询**：`vx_mpm_query` 转发异步 `vx_device_mpm_query`。`addr` 是一个 MPM CSR（落在 `[VX_CSR_MPM_BASE, +32)` 区间），`core_id == 0xffffffff` 表示跨所有核求和：

[sw/runtime/common/legacy_utils.cpp:194-196](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_utils.cpp#L194-L196)

```cpp
extern int vx_mpm_query(vx_device_h hdevice, uint32_t mpm_class, uint32_t addr,
                        uint32_t core_id, uint64_t* value) {
  return (vx_device_mpm_query(hdevice, mpm_class, addr, core_id, value)
          == VX_SUCCESS) ? 0 : -1;
}
```

**真实使用**——vecadd_v1 里「上传内核 + 上传参数 + 启动 + 等待」四连：

[tests/regression/vecadd_v1/main.cpp:172-186](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/vecadd_v1/main.cpp#L172-L186)

```cpp
RT_CHECK(vx_upload_kernel_file(device, kernel_file, &krnl_buffer));   // 上传内核
RT_CHECK(vx_upload_bytes(device, &kernel_arg, sizeof(kernel_arg_t), &args_buffer)); // 上传参数
RT_CHECK(vx_start(device, krnl_buffer, args_buffer));                 // 启动
RT_CHECK(vx_ready_wait(device, VX_MAX_TIMEOUT));                      // 等待完成
```

#### 4.3.4 代码实践

**实践目标**：把「启动」这一步在源码里走一遍，看懂 PC 和参数地址是怎么进到设备里的。

**操作步骤**：

1. 打开 [sw/runtime/common/legacy_runtime.cpp:158-234](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L158-L234)（`vx_start` 全文）。
2. 找到 `pc = kernel->dev_address()` 与 `argp = args->dev_address()` 两行（[194-195 行](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L194-L195)）。
3. 在 `kmu_writes[]` 数组里数一下：`STARTUP_ADDR0/1` 写的是 `pc` 的低/高 32 位；`STARTUP_ARG0/1` 写的是 `argp` 的低/高 32 位。
4. 对照设备端：打开 [tests/regression/vecadd_v1/kernel.cpp:12-15](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/vecadd_v1/kernel.cpp#L12-L15)，看内核如何用 `csr_read(VX_CSR_MSCRATCH)` 取回这块参数。

**需要观察的现象**：主机写的 `argp`（参数块设备地址）↔ 设备读的 `VX_CSR_MSCRATCH`，两者是同一个东西的两端。

**预期结果**：你能讲清楚「参数结构体先被 `vx_upload_bytes` 当字节上传成一块设备内存，其地址被 `vx_start` 写进 KMU ARG 寄存器，内核再经 MSCRATCH CSR 取回」这条链路。

> 运行验证（可选）：`make -C tests/regression/vecadd_v1 run-simx`，看到 `PASSED!` 即整条链路正确。能否跑通「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`vx_start` 是同步还是异步？如果连续调两次 `vx_start` 中间不插 `vx_ready_wait`，会发生什么？

> **答案**：`vx_start` 是**异步**的——它只提交并记录完成事件。连续调两次时，第二次 `vx_start` 开头会先 `legacy_take_last_event()` 取出上一次的事件并原地等待（[168-171 行](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L168-L171)），即「排空前一次飞行中的启动」。所以不会丢任务，但前一次会被隐式同步。

**练习 2**：`vx_upload_kernel_file` 为什么用 `vx_mem_reserve(min_vma, ...)` 而不是 `vx_mem_alloc`（让运行时自由选地址）？

> **答案**：因为 `.vxbin` 在编译/链接时，其代码与全局变量的地址都是按 `[min_vma, max_vma)` 这个区间固定的。只有把内核放到它指定的 VMA，里面所有绝对地址引用才正确。`vx_mem_alloc` 会返回运行时自选的地址，破坏这些引用。

**练习 3**：`vx_mpm_query` 的 `core_id` 传 `0xffffffff` 是什么意思？

> **答案**：表示「跨所有核把该计数器求和」（见 `vortex2.h` 对 `vx_device_mpm_query` 的注释）。其他值则只读指定核的计数器。

---

## 5. 综合实践

把本讲三个模块串起来，完成规格里指定的实践任务：

> **任务**：阅读 `vortex.h`，用伪代码写出「打开设备 → 分配缓冲 → 拷入数据 → 上传内核 → 启动 → 等待 → 拷回结果 → 关闭」的完整主机调用序列。

请先自己写一遍，再对照下面的参考答案（直接取自 `tests/regression/vecadd_v1/main.cpp`）。写的时候思考每一步用的是同步 API 的哪个函数、返回值怎么检查。

**参考答案（伪代码，对照 vecadd_v1）**：

```
// —— 1. 打开设备 ——
vx_dev_open(&device);                          // main.cpp:129

// —— 2. 分配缓冲 + 取设备地址（写进 kernel_arg）——
vx_mem_alloc(device, buf_size, VX_MEM_READ,  &src0_buffer);   // :142
vx_mem_address(src0_buffer, &kernel_arg.src0_addr);           // :143
vx_mem_alloc(device, buf_size, VX_MEM_READ,  &src1_buffer);   // :144
vx_mem_address(src1_buffer, &kernel_arg.src1_addr);           // :145
vx_mem_alloc(device, buf_size, VX_MEM_WRITE, &dst_buffer);    // :146
vx_mem_address(dst_buffer,  &kernel_arg.dst_addr);            // :147

// —— 3. 拷入数据（主机 → 设备）——
vx_copy_to_dev(src0_buffer, h_src0, 0, buf_size);             // :166
vx_copy_to_dev(src1_buffer, h_src1, 0, buf_size);             // :170

// —— 4. 上传内核 + 上传参数 ——
vx_upload_kernel_file(device, "kernel.vxbin", &krnl_buffer);  // :174
vx_upload_bytes  (device, &kernel_arg, sizeof(kernel_arg), &args_buffer); // :178

// —— 5. 启动 ——
vx_start(device, krnl_buffer, args_buffer);                   // :182

// —— 6. 等待完成 ——
vx_ready_wait(device, VX_MAX_TIMEOUT);                        // :186

// —— 7. 拷回结果（设备 → 主机）——
vx_copy_from_dev(h_dst, dst_buffer, 0, buf_size);             // :190

// —— 8. （校验后）关闭：释放缓冲 + dump 性能 + 关设备 ——
vx_mem_free(src0_buffer); vx_mem_free(src1_buffer);           // cleanup() :111-115
vx_mem_free(dst_buffer);  vx_mem_free(krnl_buffer); vx_mem_free(args_buffer);
vx_dump_perf(device, stdout);                                 // :116
vx_dev_close(device);                                         // :117
```

**进阶思考**：把上面每一步在 `legacy_runtime.cpp` / `legacy_utils.cpp` 里对应的「异步提交 + 等待」实现标注出来（例如 `vx_copy_to_dev` → `enqueue_and_wait(vx_enqueue_write)`，`vx_start` → 一串 DCR 写 + `vx_enqueue_launch`）。能完成这步，说明你已真正理解「同步 API 是异步 API 的薄封装」。

> 实操建议：在 build 树里 `make -C tests/regression/vecadd_v1 run-simx`，用 `printf` 在每一步前后加一行日志，观察 8 个阶段的实际顺序与耗时。具体运行结果「待本地验证」。

## 6. 本讲小结

- Vortex 运行时 API 分两层：**`vortex2.h`（异步、规范、最小化核心）** 与 **`vortex.h`（同步、薄封装、易用）**；同步 API 的每个调用本质都是「一次异步提交 + 一次事件等待」。
- 句柄（`vx_device_h` / `vx_buffer_h`）是不透明 `void*`，定义在 `vortex2.h`；能力 ID（`VX_CAPS_*`）和内存标志（`VX_MEM_*`）也在那里，**运行时查询**而非编译期宏，这是软硬件边界隔离在 API 层的体现。
- 设备生命周期与内存管理：`vx_dev_open/close`、`vx_dev_caps`、`vx_mem_alloc/address/free`、`vx_copy_to_dev/from_dev`，风格对齐 CUDA/OpenCL 的主机接口。
- 内核启动三连：`vx_upload_kernel_file`（按 `.vxbin` 自带 VMA 放置）→ `vx_start`（编程一整套 `VX_DCR_KMU_*` 寄存器并异步触发 launch）→ `vx_ready_wait`（阻塞等完成）。
- 性能查询用 `vx_mpm_query`（读单个 MPM 计数器）与 `vx_dump_perf`（打印整份报告）。
- 返回值约定：同步 API 返回 `int`（0 成功 / -1 失败），由 `to_int(vx_result_t)` 压平，代价是丢失具体错误码。

## 7. 下一步学习建议

本讲只讲了「主机侧怎么调」，没有展开对象内部与后端分发。建议接着学：

- **u3-l2 设备、缓冲区与内存管理**：深入 `device.cpp` / `buffer.cpp` / `vm.cpp`，看清 `vx_device_h`、`vx_buffer_h` 背后的对象结构与设备虚拟内存管理。
- **u3-l3 驱动后端与 stub 动态分发**：搞懂 `libvortex.so` 如何按 `$VORTEX_DRIVER` 用 `dlopen` 切换 simx/rtlsim/opae/xrt 后端，以及 `callbacks_t` 分发表。
- **u3-l4 主机→设备启动流程与 .vxbin 加载**：展开 `vx_start` 提交后「命令处理器 → KMU → CTA 派发」的完整路径，以及 `.vxbin` 的 `VXSYMTAB` 多入口符号表（异步 API `vx_module_get_kernel` 走的那条路）。

如果想横向对比，可再看一眼 `tests/regression/demo/main.cpp`（异步写法），体会同步与异步两种风格的取舍。
