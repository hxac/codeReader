# SARBackproject 类与 XRT 初始化

## 1. 本讲目标

上一讲（u3-l1）我们跟完了 `main.cpp` 的五阶段骨架，并把「构造函数内部到底做了什么」刻意留到了本讲。本讲就钻进 `SARBackproject` 的构造函数，弄清主机程序在「真正开始反投影之前」做的三件事：

1. **打开设备、加载 xclbin**：主机如何通过 XRT（Xilinx RunTime）拿到 Versal 芯片的句柄。
2. **分配并映射 buffer 对象**：那些装 slowtime / 距离压缩 / 目标像素 / 输出图像的内存是怎么来的，大小由谁决定。
3. **建立 AIE 图与 PL 内核句柄**：主机凭什么能驱动 AIE 阵列和 PL 包路由器。

学完后你应当能够：

- 读懂 `SARBackproject` 类头文件里每一组成员的职责。
- 算出每个 `xrt::bo` / `xrt::aie::bo` 的字节大小，并说清大小公式与 `common.h` 宏的关系。
- 说清 `m_img_buffers` 为什么用 `kernel.group_id(1)` 来选 DDR bank，而三个输入 buffer 却写死成 bank `0`。
- 区分 `xrt::kernel` / `xrt::run` / `xrt::graph` 三类句柄各干什么。

---

## 2. 前置知识

在读懂构造函数前，需要三个概念垫底。本节用最朴素的语言把它们讲清楚。

### 2.1 XRT（Xilinx RunTime）是什么

Versal 是一块异构芯片，主机（ARM）不能直接「戳寄存器」去调度 AIE 和 PL——那太底层、太容易出错。AMD 提供了一层叫 **XRT** 的用户态运行库，把「打开设备、加载比特流、分配显存、启动内核」这些事封装成一套 C++ API（`xrt::device`、`xrt::bo`、`xrt::kernel`、`xrt::graph` 等）。本仓库 `design/host/` 的代码几乎就是围绕这套 API 写的。对应头文件在本讲头文件里能看到：

```cpp
#include "xrt/xrt_kernel.h"
#include "xrt/xrt_graph.h"
#include "xrt/xrt_aie.h"
```

### 2.2 xclbin 是什么

`xclbin` 是 XRT 的「**一个文件装下整颗芯片要跑的东西**」的容器格式。在本项目里，`v++ -l` 把编译好的 AIE 图（`libadf.a`）和 PL 内核（`dma_pkt_router.xo`）链接成一个 `.xclbin`（见 u1-l3 的依赖链）。主机要驱动硬件，第一步就是把这个文件加载进设备。这也是为什么 `main.cpp` 把 xclbin 文件名作为第一个命令行参数。

### 2.3 buffer object（bo）：主机与设备之间的「快递箱」

主机 CPU 和 Versal 芯片（AIE / PL）不共享同一块内存——它们各自挂在 DDR 的不同区域，甚至不同 bank。要给设备喂数据，主机必须先在「设备能看见的 DDR」上申请一块缓冲，这叫 **buffer object（`xrt::bo`）**。然后：

- 用 `.map<T*>()` 把这块 DDR **映射**成主机进程里的一个指针，CPU 就能像普通数组一样读写它。
- 用 `.async(...)` / `.sync(...)` 在主机内存与设备之间搬运数据。

本项目里有两种 bo：

| 类型 | 含义 | 本项目用于 |
|------|------|-----------|
| `xrt::aie::bo` | 走 **GMIO**（DDR↔AIE，经 NoC）的 buffer | slowtime / RC / 目标像素三类输入 |
| `xrt::bo` | 通用 buffer，可被 PL 内核的 `m_axi` 接口直接寻址 | 输出图像（由 PL 包路由器写入） |

记住这张区分，它是后面理解「为什么输入 buffer 写死 bank 0、而图像 buffer 用 `group_id(1)`」的关键。

> 名词速查：
> - **bank / memory group**：Versal 的 DDR 被分成若干「bank」（物理通道），不同 bank 可并行访问。把一块 buffer 放在哪个 bank，会影响带宽和一致性。
> - **GMIO / PLIO**：详见 u2-l2。GMIO = DDR↔AIE 的 DMA 通道（过 NoC）；PLIO = AIE↔PL 的 AXI4-Stream 直连（不过 NoC）。
> - **句柄（handle）**：一个代表「设备上某个资源」的不透明对象，本讲里就是 `xrt::kernel` / `xrt::run` / `xrt::graph`。

---

## 3. 本讲源码地图

本讲只涉及主机侧两个文件（外加 PL 头文件作交叉参照）：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `design/host/sar_backproject.h` | `SARBackproject` 类声明 | 全部成员变量与构造函数签名 |
| `design/host/sar_backproject.cpp` | 类的实现 | 构造函数（静态成员定义 + 初始化列表 + 函数体） |
| `design/host/main.cpp` | 程序入口 | 构造函数是如何被实例化的（u3-l1 已讲，这里只回顾调用点） |
| `design/pl/dma_pkt_router.h` | PL 包路由器接口 | 仅看函数签名，解释 `group_id(1)` 为什么是 1 |
| `design/common.h` | 全局配置宏 | buffer 大小公式里的 `PULSES` / `RC_SAMPLES` / `BC_ELEMENTS` |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应构造函数里依次完成的三件事。

### 4.1 SARBackproject 类成员

#### 4.1.1 概念说明

`SARBackproject` 是一个把「整个反投影流程」封装起来的类。它的成员变量可以理解成三类「家当」：

1. **配置参数**：xclbin 文件名、数据集文件名、迭代次数、实例数——这些是构造时从 `main` 传进来的「身份信息」。
2. **设备资源句柄**：`m_device`、`m_uuid`、AIE 图句柄、PL 内核句柄——这些是构造函数「打开设备、加载 xclbin」之后从 XRT 拿到的资源。
3. **数据 buffer 与映射指针**：成对出现的 `xxx_buffer`（bo 对象）和 `xxx_array`（映射后的 CPU 指针），供后续取数与反投影使用。

把它想成「一个厨师」：配置参数是菜谱，设备句柄是厨房钥匙和灶台编号，buffer 是备好的食材盘。

#### 4.1.2 核心流程

类成员的生命周期大致是：

```
main() 传入 5 个参数 + INSTANCES=1
   │
   ▼
构造函数：保存参数 → 打开设备 → 申请 buffer → 建内核/图句柄
   │
   ▼
对象 ifcc 就绪，后续 fetchRadarData / genTargetPixels / bp / writeImg 都用它的成员
```

注意：成员在头文件里声明，**静态成员**还需要在 `.cpp` 里有且仅有一处定义（下一模块会讲）。

#### 4.1.3 源码精读

先看头文件里的成员声明（按声明顺序）：私有的配置参数与设备句柄在

[design/host/sar_backproject.h:17-38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L17-L38)

其中几行值得单独点出：

- `const char* m_xclbin_filename;` 等四个文件名指针（[L18-L21](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L18-L21)）——分别对应 xclbin、slowtime CSV、RC CSV、图像输出 CSV。
- `const int m_iter;` 与 `const int m_instances;`（[L25](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L25) 与 [L28](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L28)）——`m_iter` 来自 `argv[5]`，但如 u3-l1 所述，多次迭代路径目前并未真正走通；`m_instances` 在 `main.cpp` 里被硬编码成 `INSTANCES = 1`。
- `xrt::device m_device;` 与 `xrt::uuid m_uuid;`（[L31-L32](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L31-L32)）——设备句柄与「这次加载的 xclbin 的唯一编号」，后续每次创建内核/图都要带上这个 uuid，证明「我加载过这个比特流」。
- `hid_t file;`（[L35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L35)）——这是 HDF5 类型（`#include <hdf5.h>`），声明了却**在当前主流程里没有被使用**，属于历史遗留/为其他数据格式预留的字段。读者可以用 `grep` 自行确认。
- `std::vector<xrt::graph> m_bp_graph_hdls;`（[L38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L38)）——AIE 图句柄用 vector 存，是为了支持「多实例」。

然后是公开的输入 buffer 及其映射指针：

[design/host/sar_backproject.h:41-47](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L41-L47)

可以看到三组「bo + 指针对」：slowtime 广播数据（`m_broadcast_data_buffer` / `..._array`，`float`）、目标像素（`m_xyz_px_buffer` / `..._array`，`float`）、距离压缩数据（`m_rc_buffer` / `..._array`，`cfloat` 复数）。注意它们都是 **`xrt::aie::bo`**（走 GMIO），而下面 PL 侧的图像 buffer 是普通 `xrt::bo`。

PL 包路由器的内核/运行/图像 buffer 句柄在头文件靠后位置（被声明为 public 实例变量）：

[design/host/sar_backproject.h:77-87](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L77-L87)

- `std::vector<xrt::kernel> m_dma_pkt_router_kernels;`——每个 PL 包路由器实例一个内核对象。
- `std::vector<xrt::run> m_dma_pkt_router_run_hdls;`——每个内核对应一个「运行句柄」，用于 `set_arg/start`。
- `std::vector<xrt::bo> m_img_buffers;` 与 `std::vector<cfloat*> m_img_arrays;`——输出图像 buffer 与映射指针。
- 静态计时变量 `total_time` / `total_avg_time` / `time_start` / `time_end`（[L84-L87](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L84-L87)）——u3-l1 已讲过其用途，本讲关注它们的**定义**位置。

#### 4.1.4 代码实践

**实践目标**：把头文件里的成员按「家当三分类」归档，并找出「声明了但当前没用」的字段。

**操作步骤**：

1. 打开 `design/host/sar_backproject.h`，通读 L15–L89。
2. 列一张表，把每个成员归入「配置参数 / 设备句柄 / 数据 buffer」三类之一。
3. 对可疑字段（如 `hid_t file;`、`m_iter`）用 `grep` 在 `sar_backproject.cpp` 里搜索其使用次数：

```
# 在仓库根目录执行（只读检索）
grep -n "m_iter" design/host/sar_backproject.cpp
grep -n "file" design/host/sar_backproject.cpp | head
```

**需要观察的现象**：`m_iter` 在 `.cpp` 里除了初始化列表赋值外，是否还有被读取的地方；`hid_t file` 是否完全没有出现。

**预期结果**：`m_iter` 仅在构造函数初始化列表里被赋值，主流程并未真正按它循环（与 u3-l1 结论一致）；`hid_t file` 基本未被使用。这帮你建立「源码里有为未来/其他分支预留的预埋字段」的判断力。

> 说明：以上 `grep` 是源码阅读检索，不需要硬件即可完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 AIE 图句柄 `m_bp_graph_hdls`、PL 内核句柄都用 `std::vector` 存，而三个输入 buffer 却是单个对象？

**参考答案**：因为设计支持「实例数 `m_instances`」可配——每个实例都对应一组独立的图与 PL 内核（一实例一套句柄），所以用 vector 按实例扩展；而三个输入 buffer 在「单实例」语义下只有一份（slowtime/rc/像素各一块 DDR），所以是单个对象。当前 `INSTANCES=1`，vector 里实际只有一个元素。

**练习 2**：`m_device` 和 `m_uuid` 为什么都要存成成员？

**参考答案**：`m_device` 是「设备的门」，后续所有申请 buffer、建内核都要带上它；`m_uuid` 是「这次加载的 xclbin 的身份证」，创建内核/图时 XRT 要求同时提供 device 和 uuid，以确认这些资源确实属于当前加载的比特流。两者在构造期之后还会被反复使用，故存为成员。

---

### 4.2 device / load_xclbin / xrt::bo 的构造

#### 4.2.1 概念说明

这个模块讲构造函数**初始化列表**的前半段：打开设备、加载 xclbin、申请三个输入 buffer 并映射。C++ 的成员初始化列表有一个关键性质：**成员按「在类中声明的顺序」初始化，而不是按出现在初始化列表里的顺序**。所以你会看到，初始化列表严格按照头文件的声明顺序书写——这一点对本类尤其重要，因为后面的成员依赖前面的成员（buffer 依赖 `m_device`，映射指针依赖 buffer）。

构造函数的「重头戏」其实是一行链式依赖：

- `m_device(0)` → 打开 0 号设备；
- `m_uuid(m_device.load_xclbin(...))` → 用刚打开的设备加载 xclbin，拿到 uuid；
- 三个 buffer 都用 `(m_device, 大小, flags, bank)` 构造 → 依赖 `m_device`；
- 三个映射指针用 `buffer.map<T*>()` 构造 → 依赖各自的 buffer。

只要顺序错了，编译可能过、运行会崩。

#### 4.2.2 核心流程

三个输入 buffer 的大小都由 `common.h` 宏决定（宏的含义见 u1-l4）：

| buffer | 元素类型 | 大小公式 | 字节数（默认宏） |
|--------|---------|---------|-----------------|
| `m_broadcast_data_buffer` | `float` | \( \text{PULSES} \times \text{BC\_ELEMENTS} \times \text{sizeof(float)} \) | \( 602 \times 4 \times 4 = 9632 \) B ≈ 9.4 KiB |
| `m_xyz_px_buffer` | `float` | \( \text{PULSES} \times \text{RC\_SAMPLES} \times \text{sizeof(float)} \times 3 \) | \( 602 \times 512 \times 4 \times 3 = 3\,696\,640 \) B ≈ 3.53 MiB |
| `m_rc_buffer` | `cfloat` | \( \text{PULSES} \times \text{RC\_SAMPLES} \times \text{sizeof(cfloat)} \) | \( 602 \times 512 \times 8 = 2\,464\,768 \) B ≈ 2.35 MiB |

说明：

- `sizeof(float) = 4`，`sizeof(cfloat) = 8`（`cfloat` 是 `{float real; float imag;}`，两个 float）。
- 三个输入 buffer 构造时第 4 个参数都写 `0`，表示放在 **memory bank 0**。
- `m_xyz_px_buffer` 的 `×3` 对应每个目标像素的 X/Y/Z 三个坐标（见 `genTargetPixels` 里 `idx++` 三次）。
- `BC_ELEMENTS = 4` 对应 slowtime 每行 4 列（天线 X/Y/Z + ref_range，见 u1-l2）。

核心流程可概括为：

```
m_device(0)                       // 打开 0 号设备
   └─ m_device.load_xclbin(name)  // 加载 xclbin → m_uuid
         └─ 三个 xrt::aie::bo(m_device, size, normal, bank=0)
               └─ 各自 .map<T*>() → CPU 可直接读写的指针
```

#### 4.2.3 源码精读

先看 `.cpp` 顶部的**静态成员定义**——这是 C++ 规则：静态成员必须在类外定义一次：

[design/host/sar_backproject.cpp:15-18](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L15-L18)

这四行把 `total_time`、`total_avg_time`、`time_start`、`time_end` 初始化为 0，是 u3-l1 计时机制能工作的前提。

接着是构造函数签名与本模块的主角——初始化列表前半段：

[design/host/sar_backproject.cpp:20-39](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L20-L39)

逐行拆解：

- [L32](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L32) `m_device(0)`：这里的 `0` 是**设备索引**（device index 0），不是 bank。意思是「打开系统里第 0 块 Versal 设备」。
- [L33](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L33) `m_uuid(m_device.load_xclbin(this->m_xclbin_filename))`：在刚打开的设备上加载 xclbin，返回值存进 `m_uuid`。注意它依赖上一行的 `m_device` 已经构造好——这正是「初始化顺序必须与声明顺序一致」的现实意义。
- [L34](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L34) `m_broadcast_data_buffer(m_device, PULSES*BC_ELEMENTS*sizeof(float), xrt::bo::flags::normal, 0)`：slowtime buffer。第 4 个参数 `0` 是 **memory group/bank**。`xrt::bo::flags::normal` 表示普通缓存属性。
- [L35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L35) `m_broadcast_data_array(m_broadcast_data_buffer.map<float*>())`：把这块 DDR 映射成 `float*`，之后 `fetchRadarData` 就往这个指针写 slowtime。
- [L36-L37](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L36-L37)：目标像素 buffer 与映射，注意大小多了 `*3`（X/Y/Z）。
- [L38-L39](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L38-L39)：RC buffer 与映射，元素是 `cfloat`。

> 小贴士：`.map<T*>()` 返回的指针背后是**内存映射（mmap）**，CPU 读写它就是在读写设备 DDR 上的那块 buffer，省去了一次显式拷贝。

#### 4.2.4 代码实践

**实践目标**：验证三个输入 buffer 的字节大小，并体会「初始化列表顺序 = 声明顺序」的约束。

**操作步骤**：

1. 用下面的「示例代码」（非项目代码）计算默认配置下三个 buffer 的大小：

```python
# 示例代码：仅用于核对 buffer 字节数，非项目源码
PULSES = 602
RC_SAMPLES = 512
BC_ELEMENTS = 4
SIZEOF_FLOAT = 4
SIZEOF_CFLOAT = 8   # cfloat = real(float) + imag(float)

broadcast = PULSES * BC_ELEMENTS * SIZEOF_FLOAT
xyz_px    = PULSES * RC_SAMPLES * SIZEOF_FLOAT * 3
rc        = PULSES * RC_SAMPLES * SIZEOF_CFLOAT

print(f"broadcast = {broadcast} B = {broadcast/1024:.1f} KiB")
print(f"xyz_px    = {xyz_px} B = {xyz_px/(1024*1024):.2f} MiB")
print(f"rc        = {rc} B = {rc/(1024*1024):.2f} MiB")
```

2. 对照 [sar_backproject.cpp:34-38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L34-L38)，确认公式与代码一致。
3. 思考实验：如果把初始化列表里 `m_uuid(...)` 挪到 `m_device(0)` 之前，会发生什么？

**需要观察的现象**：脚本的输出字节数；以及第 3 步里你会意识到「`load_xclbin` 需要一个已构造好的 `m_device`」，顺序错了就是用一个还没构造的设备去加载比特流。

**预期结果**：输出约为 `broadcast = 9632 B = 9.4 KiB`、`xyz_px ≈ 3.53 MiB`、`rc ≈ 2.35 MiB`。第 3 步的结论是：即便编译器允许你那样写，`m_device` 仍会先于 `m_uuid` 被初始化（因为声明顺序如此），所以这里只是「看着错了实际没事」；但若类声明里把 `m_uuid` 写在 `m_device` 之前，就会真正出问题——这正是「声明顺序决定初始化顺序」的陷阱。

> 说明：上述 Python 是示例代码，运行它不需要 Versal 硬件。

#### 4.2.5 小练习与答案

**练习 1**：`m_rc_buffer` 的大小为什么是 `PULSES*RC_SAMPLES*sizeof(cfloat)`，而 `m_broadcast_data_buffer` 只有 `PULSES*BC_ELEMENTS*sizeof(float)`？

**参考答案**：RC 数据是一个 `PULSES × RC_SAMPLES` 的复数矩阵（每个脉冲有 `RC_SAMPLES` 个距离压缩复数样本），所以是两者相乘再乘 `sizeof(cfloat)=8`；而 slowtime 每个脉冲只有 `BC_ELEMENTS=4` 个实数（天线 X/Y/Z + ref_range），所以是 `PULSES × 4 × sizeof(float)`。两者的「列数」差别正是 RC=512 与 BC=4。

**练习 2**：三个输入 buffer 都把 bank 写成 `0`，这意味着什么？会不会是瓶颈？

**参考答案**：意味着三块输入数据都落在 DDR 的 bank 0 上。因为它们走 GMIO（DDR↔AIE），而本设计把 GMIO 通道绑定到 bank 0，所以 buffer 也必须放 bank 0 才能被对应 GMIO 通道访问。单实例、顺序投递时不会成为瓶颈；但若多实例并发，都挤 bank 0 可能竞争带宽——这也是「多实例」尚未铺平的障碍之一。

---

### 4.3 xrt::graph 与 xrt::kernel 句柄的建立

#### 4.3.1 概念说明

构造函数**函数体**（初始化列表之后的大括号）里，做的是「按实例数循环，建立 PL 内核句柄、图像 buffer 和 AIE 图句柄」。这里出现三组需要区分清楚的 XRT 句柄：

- **`xrt::kernel`**：绑定到「设备上某个内核函数 + 某个具名实例」的对象。本项目里每个 PL 包路由器实例对应一个 `xrt::kernel`。
- **`xrt::run`**：从 `xrt::kernel` 创建的「一次运行的把手」，可以 `set_arg(...)` 设参数、`start()` 启动、`wait()` 等结束。一个 kernel 可以创建多个 run（本项目一对一）。
- **`xrt::graph`**：AIE 数据流图的运行句柄，用来 `run(0)` 启动、`update(...)` 改 RTP、`wait()` 等完成（详见 u2-l3、u3-l5）。

这个模块还要回答本讲的关键问题：**`m_img_buffers` 为什么用 `kernel.group_id(1)` 选 bank？**

#### 4.3.2 核心流程

构造函数体里的双层循环结构：

```
for i in 0..m_instances:            // 每个设计实例
    for sw_id in 0..AIE_SWITCHES:   // 每个 AIE switch → 一个 PL 包路由器
        kernel 名 = "dma_pkt_router:{dma_pkt_router_<sw_id>}"
        push 到 m_dma_pkt_router_kernels
        push 一个 xrt::run(kernel) 到 m_dma_pkt_router_run_hdls

    // 图像 buffer（每个实例一块）
    push xrt::bo(m_device, PULSES*RC_SAMPLES*8, kernels[0].group_id(1)) 到 m_img_buffers
    push m_img_buffers[i].map<cfloat*>() 到 m_img_arrays

    // AIE 图句柄（每个实例一张）
    push xrt::graph(m_device, m_uuid, "bpGraph[i]") 到 m_bp_graph_hdls
```

关于内核命名 `"dma_pkt_router:{dma_pkt_router_0}"`：这是 XRT 的「`<内核名>:{<实例名>}`」语法。`dma_pkt_router_0`…`dma_pkt_router_6` 这 7 个实例名，正是 `v++ -l` 阶段由 `system.cfg` 的 `nk=` 行声明的（见 u7-l1）。换句话说，主机这里的字符串必须和链接时声明的实例名**一一对应**，否则 `xrt::kernel` 构造会找不到内核。

关于 `group_id(1)`：`1` 是内核函数参数的下标。看 PL 内核签名：

```cpp
int dma_pkt_router(hls::stream<ap_axiu<128,0,0,0>> &pl_stream_in,   // arg 0
                   ap_uint<64>* ddr_mem);                            // arg 1
```

`ddr_mem` 是第 1 个参数（下标从 0 算），它在 PL 侧用 `m_axi ... bundle=gmem` 绑到了某个 DDR bank。`kernel.group_id(1)` 就是去问「这个内核的第 1 号参数被绑到了哪个 memory group/bank」，然后让图像 buffer 也分配在**同一个 bank** 上——这样 PL 内核写出的图像数据才能落到主机能 `sync` 回来的那块 buffer。代码注释也明确说：所有内核实例都映射到同一个 bank，所以用 `kernels[0]` 问一次就够。

#### 4.3.3 源码精读

构造函数体在：

[design/host/sar_backproject.cpp:40-63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L40-L63)

关键点逐段看：

**PL 内核与 run 句柄的创建**（注意 TODO 注释里坦诚「instances > 1 尚未修好」）：

[design/host/sar_backproject.cpp:46-51](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L46-L51)

- [L48](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L48)：拼接出 `"dma_pkt_router:{dma_pkt_router_<sw_id>}"`，`sw_id` 从 0 到 `AIE_SWITCHES-1`（默认 6），共 7 个内核。
- [L49](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L49)：`xrt::kernel(m_device, m_uuid, name)`——device、uuid、内核名三件套，缺一不可。
- [L50](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L50)：从 kernel 创建 `xrt::run`，后面 `bp()` 里会用它 `set_arg(1, ...)` 与 `start()`。

**图像 buffer 与映射（本讲的核心问题）**：

[design/host/sar_backproject.cpp:53-58](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L53-L58)

- [L57](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L57)：`xrt::bo(m_device, PULSES*RC_SAMPLES*8, m_dma_pkt_router_kernels[0].group_id(1))`。
  - 大小 `PULSES*RC_SAMPLES*8`：注意这里写成了字面量 `8`，它等价于 `sizeof(cfloat)`（复数图像，每像素 8 字节）。与上面 `m_rc_buffer` 用 `sizeof(cfloat)` 相比，这里是「魔数」写法——值相同，但可读性略差。
  - 第 3 个参数不再是写死的 `0`，而是 `kernels[0].group_id(1)`：**让 PL 内核自己决定图像 buffer 落在哪个 bank**。
- [L58](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L58)：映射成 `cfloat*`，供 `writeImg()` 读出图像。

**AIE 图句柄的创建**：

[design/host/sar_backproject.cpp:60-61](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L60-L61)

- 图名 `"bpGraph[0]"` 必须和 ADF 图编译时的顶层图名匹配（详见 u4-l1 的 `BackProjectionGraph`）。`xrt::graph(device, uuid, name)` 三件套与 kernel 完全对称。

为了让你确信 `group_id(1)` 里的 `1` 从何而来，看 PL 内核签名与接口 pragma：

[design/pl/dma_pkt_router.h:15-16](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.h#L15-L16)

以及对应的 m_axi 绑定（确认 `ddr_mem` 是 arg 1，并绑到 `gmem` bundle）：

[design/pl/dma_pkt_router.cpp:11-16](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L11-L16)

`#pragma HLS INTERFACE m_axi port=ddr_mem ... bundle=gmem` 这行决定了 `ddr_mem` 走哪个 DDR bank；主机侧 `group_id(1)` 正是去查询这个绑定，从而把图像 buffer 放到匹配的 bank。

#### 4.3.4 代码实践

**实践目标**：解释 `m_img_buffers` 为什么用 `group_id(1)`，并对比它与三个输入 buffer 的 bank 选择差异。

**操作步骤**：

1. 打开 [sar_backproject.cpp:57](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L57)，找到 `group_id(1)`。
2. 打开 [dma_pkt_router.cpp:11-16](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L11-L16)，数一下 `ddr_mem` 是第几个参数（下标 0 是 `pl_stream_in`，下标 1 是 `ddr_mem`），确认 `group_id(1)` 的 `1` 对应它。
3. 回答下面两问（写在你的学习笔记里）：
   - 为什么三个**输入** buffer（broadcast / xyz / rc）可以写死 bank `0`，而**输出**图像 buffer 却要 `group_id(1)`？
   - 如果把 `group_id(1)` 改成写死的 `0`，可能会出什么问题？

**需要观察的现象**：输入 buffer 是 `xrt::aie::bo`（走 GMIO，bank 由 GMIO 通道绑定决定，本设计都绑 bank 0）；输出图像 buffer 是普通 `xrt::bo`（被 PL 内核的 `m_axi` 直接寻址），bank 由 `m_axi` 的 `bundle=gmem` 在链接时决定。

**预期结果**：
- 输入 buffer 必须与 GMIO 通道同 bank（都是 0），所以写死 `0` 正确且直观。
- 图像 buffer 必须与 PL 内核 `ddr_mem` 的 m_axi 绑定同 bank；这个 bank 不一定是 0，取决于 `system.cfg` / `v++ -l` 如何分配。用 `group_id(1)` 是「运行时去问内核」，比硬编码更稳健——若链接器把 gmem 分到别的 bank，硬编码 `0` 会导致 PL 写入的 bank 与主机 sync 的 bank 不一致，图像数据对不上。
- 待本地验证：在真实 VCK190 上打印 `kernels[0].group_id(1)` 的返回值，确认它确实等于 gmem 所在 bank。

#### 4.3.5 小练习与答案

**练习 1**：`xrt::kernel` 和 `xrt::run` 有什么区别？为什么本项目要各存一份 vector？

**参考答案**：`xrt::kernel` 描述「是哪个内核、在哪个实例上」，是相对静态的；`xrt::run` 描述「一次具体的运行」，可以设参数、启动、等待，是动态的。一个 kernel 可派生多个 run。本项目对每个 PL 包路由器实例都建了一个 kernel 和一个 run（一一对应），分别存进 `m_dma_pkt_router_kernels` 与 `m_dma_pkt_router_run_hdls`：kernel 还被用来查询 `group_id(1)`，run 则在 `bp()` 里被 `set_arg/start`。

**练习 2**：内核名字字符串 `"dma_pkt_router:{dma_pkt_router_3}"` 里的 `dma_pkt_router_3` 是哪来的？如果改了它会怎样？

**参考答案**：`dma_pkt_router_3` 是该 PL 内核实例的**实例名**，由链接阶段 `system.cfg` 的 `nk=dma_pkt_router:7:dma_pkt_router_0.dma_pkt_router_1....` 这类声明产生（见 u7-l1）。主机字符串必须与之精确匹配，否则 `xrt::kernel` 构造时 XRT 找不到该实例会报错。所以「主机字符串 ↔ system.cfg 实例名 ↔ AIE_SWITCHES」三者必须一致。

---

## 5. 综合实践

把三个模块串起来，完成一张「构造函数全景标注表」。

**任务**：通读 [sar_backproject.cpp:20-63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L20-L63) 的整个构造函数，制作一张表，包含以下四列：

| 成员 | 类型 | 大小公式（字节数） | bank 选择 | 谁会读写它 |

要求填入至少这些行：`m_device`、`m_uuid`、`m_broadcast_data_buffer`、`m_xyz_px_buffer`、`m_rc_buffer`、`m_dma_pkt_router_kernels`、`m_dma_pkt_router_run_hdls`、`m_img_buffers`、`m_bp_graph_hdls`。

**提示与验收点**：

1. `m_device` / `m_uuid` 不是 buffer，大小列写「—」，bank 列写「—」，但要写清它们的作用（设备门 / xclbin 身份证）。
2. 三个输入 buffer 的 bank 列填 `0`（写死），`m_img_buffers` 填 `kernels[0].group_id(1)`（由 PL 内核决定）——并在备注里写一句话解释两者为何不同。
3. 「谁会读写它」一列：例如 `m_broadcast_data_buffer` 由 `fetchRadarData()` 写入、由 `bp()` 经 GMIO 投递给 AIE 读；`m_img_buffers` 由 PL 包路由器写入、由 `bp()` 的 `sync(FROM_DEVICE)` 读回主机、再由 `writeImg()` 写成 CSV。
4. 在表下方，用一段话回答本讲的统领问题：**「构造函数一共向设备申请了多少字节的主机可见 DDR？其中输入侧多少、输出侧多少？」**

**参考量**（输入侧）：\( 9632 + 3\,696\,640 + 2\,464\,768 \approx 5.88 \) MiB；输出图像侧单实例 \( 2\,464\,768 \approx 2.35 \) MiB。把这些数和你的表格对一遍。

> 说明：本实践是源码阅读 + 手动核算型，不需要 Versal 硬件即可完成；若能在真实板卡上跑通 `sar_backproject.elf`，可进一步用 `printTimeDiff("Init completed (HOST)")` 的输出来感受「打开设备 + 加载 xclbin + 建 buffer/句柄」这一阶段相对后续取数阶段的耗时占比（通常初始化远快于 CSV 取数，参见 u8-l2）。

---

## 6. 本讲小结

- `SARBackproject` 把整个反投影流程封装成一个类，成员可分三类：配置参数、设备资源句柄、数据 buffer 与映射指针；其中 `m_iter` 与 `hid_t file` 属于「声明了但主流程未充分使用」的预埋字段。
- 构造函数用初始化列表依次完成 `m_device(0)` → `load_xclbin` → 三个输入 `xrt::aie::bo` → 各自 `.map<T*>()`；**初始化顺序由类内声明顺序决定**，列表书写顺序必须与之保持一致以避免依赖错乱。
- 三个输入 buffer 的大小都由 `common.h` 宏决定：广播 `PULSES·BC_ELEMENTS·4`、目标像素 `PULSES·RC_SAMPLES·4·3`、RC `PULSES·RC_SAMPLES·8`，三者都落 bank 0（与 GMIO 通道绑定匹配）。
- 函数体里按实例数循环建立 PL 句柄：`xrt::kernel`（内核对象）、`xrt::run`（运行把手）、`xrt::graph`（AIE 图把手）；内核名字必须与 `system.cfg` 的实例名一一对应。
- **核心结论**：`m_img_buffers` 用 `kernels[0].group_id(1)` 选 bank，是因为图像 buffer 是被 PL 内核 `ddr_mem`（arg 1，`m_axi bundle=gmem`）直接寻址的普通 `xrt::bo`，bank 必须与 m_axi 绑定一致；而三个输入 `xrt::aie::bo` 走 GMIO，bank 由 GMIO 通道绑定决定（写死 0）。`group_id(1)` 是「运行时问内核」的稳健写法，优于硬编码。

---

## 7. 下一步学习建议

构造函数把所有句柄和 buffer 都备好了，下一步就是「往输入 buffer 里填数据」。建议继续：

- **u3-l3 从 CSV 读取雷达数据**：深入 `fetchRadarData()`，看它如何把 slowtime 与 RC 两类 CSV 解析进本讲建立的 `m_broadcast_data_array` 与 `m_rc_array`——正好用上本讲算出的 buffer 大小与 `BC_ELEMENTS`、`RC_SAMPLES` 的含义。
- **u3-l5 用 XRT 编排 AIE 图与 PL 内核**：看 `bp()` 如何使用本讲建立的 `m_bp_graph_hdls`、`m_dma_pkt_router_run_hdls`、`m_img_buffers`，把数据真正喂进 AIE 并取回图像。
- 若想提前理解 PL 包路由器「为什么需要重排图像」，可先跳读 **u6-l1 PL 包路由器 HLS 内核**，再回头看本讲的 `group_id(1)` 会更有体感。
