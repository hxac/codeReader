# buffer object、同步与存储分区

## 1. 本讲目标

上一讲（u4-l2）我们用「device → load_xclbin → kernel → run」四步跑通了主机对内核的控制，并顺带见到了 `xrt::bo`（buffer object）——但当时把它当作一个不透明的「设备内存缓冲」一带而过。本讲就把这个黑盒彻底打开。

学完本讲，你应该能够：

- 说出 `xrt::bo` 是什么、为什么需要它，并用 `map<>()` 拿到主机可读写指针；
- 解释 `XCL_BO_SYNC_BO_TO_DEVICE` 与 `XCL_BO_SYNC_BO_FROM_DEVICE` 两个同步方向的物理含义，并知道何时该调用哪个；
- 理解 `group_id(arg)` 与内核 `m_axi` 端口的对应关系——为什么创建 bo 时要把 `group_id(0)` 作为第三个参数；
- 认识 DDR / HBM 多 bank 分区对带宽的意义，能从 `system.cfg` 的 `sp=` 行读出「哪个内核挂到了哪块存储」。

本讲继续以 `dsp/L2/examples/vss_fft_ifft_1d/host.cpp` 为蓝本，承接 u4-l2 建立的主机控制链。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**主机内存与设备内存是两块不同的存储。** 加速卡（Alveo / Versal）有自己的板载内存——数据中心卡上是大容量 DDR4，高端卡上是高带宽的 HBM2，嵌入式 Versal 上常是 LPDDR4。主机 CPU 的内存和卡上的内存物理上不共享。所谓「把数据喂给内核」，本质是把数据从主机内存搬进设备内存，内核算完，再把结果搬回主机内存。`xrt::bo` 就是这段「设备内存」在主机程序里的抽象句柄。

**一块卡上往往有多片存储（多 bank）。** 例如 U280 有 2 片 DDR，U50 有 1 片 DDR + 1 片 HBM，而 HBM 本身在逻辑上被切成几十个 bank。每片/bank 对外有一个独立的 AXI 端口。如果一个内核的读写都挤在同一个端口上，带宽就是上限；若能把输入、输出分散到不同 bank，带宽就能近似线性叠加。这就是「存储分区（memory partition / banking）」要解决的问题。

> 名词速查：
> - **bo**：buffer object，XRT 对「设备内存里一段连续缓冲」的封装。
> - **map**：把 bo 映射成主机进程里的裸指针，让 CPU 能像访问数组一样读写。
> - **sync**：在主机视图与设备视图之间搬运数据（并保证缓存一致性）。
> - **group_id**：内核某个参数对应的「存储端口组」编号，决定 bo 落在哪片存储。
> - **m_axi**：HLS 里的内存映射 AXI 主端口，内核通过它访问 DDR/HBM/LPDDR。
> - **bank / 端口**：一片可独立访问的存储及其对外端口，如 `DDR[0]`、`HBM[0]`、`LPDDR`。

本讲默认你已经读过 u4-l2，知道 `xrt::device / xrt::kernel / xrt::run` 的用法，以及 `set_arg / start / wait` 的执行模型。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `dsp/L2/examples/vss_fft_ifft_1d/host.cpp` | 主机程序。本讲几乎所有代码都来自这里：bo 的创建、map、set_arg、sync、结果校验。 |
| `dsp/L2/examples/vss_fft_ifft_1d/system.cfg` | 链接期连接配置。其中的 `sp=` 行把 mm2s/s2mm 的内存端口绑到 `LPDDR`——这是 group_id 的「物理来源」。 |
| `dsp/L1/tests/hw/mm2s/mm2s.cpp` | PL 数据搬运内核（DDR→流）。其 `mm2s_wrapper` 的参数顺序决定了 `group_id(0)` 指向哪个参数。 |
| `dsp/L1/tests/hw/s2mm/s2mm.cpp` | PL 数据搬运内核（流→DDR）。`s2mm_wrapper` 同理。 |
| `utils/ext/xcl2/xcl2.hpp` | OpenCL 主机辅助库。其中的 `aligned_allocator` 解释了「为什么主机指针要页对齐」，是理解零拷贝 map 的背景。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「bo 诞生 → 数据跨边界流动 → bo 挂到哪个端口 → 多端口如何提升带宽」的顺序展开。

### 4.1 buffer object 的创建与 map 主机映射

#### 4.1.1 概念说明

`xrt::bo`（buffer object）是 XRT 对**设备内存中一段连续缓冲**的封装。主机不能直接 `new` 一段设备内存——必须通过 `xrt::bo` 向运行时申请。拿到 bo 之后，主机还要能读写它，这就需要 `map<>()`：它把设备缓冲映射成主机进程里的一个裸指针，于是 CPU 可以像访问普通数组一样 `ptr[i] = ...`。

bo 与 map 解决的是同一个问题的两端：

- **bo** 解决「这块缓冲在哪」——它在设备内存里，且绑定到了一个具体的存储 bank（见 4.3）；
- **map** 解决「主机怎么够得着它」——给 CPU 一个可读写的主机侧视图。

#### 4.1.2 核心流程

```
申请 bo:   xrt::bo(device, 字节数, group_id)   → 返回 bo 句柄，在 device 的 group_id 号 bank 上分配
映射主机:  auto* p = bo.map<T*>()              → CPU 得到一个 T* 指针
主机读写:  p[i] = ...; ... = p[i];             → 像普通数组
(可选)同步: 见 4.2
```

注意三个要点：

1. `xrt::bo` 的**第二个参数是字节数**，不是元素个数——本例用的是 `DDR_BUFFSIZE_*_BYTES`。
2. `map` **不搬运数据**，它只是建立主机视图；真正的搬运由 `sync`（4.2）负责。
3. `map<real_dtype*>()` 的模板参数决定主机侧按什么类型解释这块内存——本例是 `real_dtype`（`int32_t`，因为 I/O 是 cint32 的实部/虚部分量）。

#### 4.1.3 源码精读

本例创建两个 bo：`mm2s_bo` 装输入激励，`s2mm_bo` 装内核回写的结果。两者都用三参数构造函数，第三个参数是 `group_id(0)`（4.3 详述）：

[dsp/L2/examples/vss_fft_ifft_1d/host.cpp:147-152](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L147-L152) —— 用 `mm2s.group_id(0)` 指定的 bank 创建输入 bo，字节数 `DDR_BUFFSIZE_I_BYTES`，随后 `map<real_dtype*>()` 拿到主机指针。

[dsp/L2/examples/vss_fft_ifft_1d/host.cpp:154-160](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L154-L160) —— 对 `s2mm_bo` 做同样的事，字节数 `DDR_BUFFSIZE_O_BYTES`，挂在 `s2mm.group_id(0)`。

字节数本身在文件上半部分由模板常量算出，注意它把「cint32 = 2 个 32-bit 分量」和「迭代次数 NITER」都折算进去了：

[dsp/L2/examples/vss_fft_ifft_1d/host.cpp:95-99](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L95-L99) —— `NUM_SAMPLES_*` 已经乘 2（实部+虚部），`DDR_BUFFSIZE_*_BYTES` 再乘 4（每个 32-bit 分量 4 字节）并乘 `NITER`。这是 bo 字节数的完整推导。

拿到主机指针后，主机用普通下标填充输入数据：

[dsp/L2/examples/vss_fft_ifft_1d/host.cpp:172-182](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L172-L182) —— 从 `input_front.txt` 读激励，按实部/虚部交替写入 `mm2s_bo_mapped[ss]` 与 `mm2s_bo_mapped[ss+1]`。

> 旁注：本例在填充 `mm2s_bo_mapped` 之后**没有**调用 `sync(XCL_BO_SYNC_BO_TO_DEVICE)`（见 4.2 的解释——这是因为本例编译目标是嵌入式 Versal，map 是零拷贝）。

#### 4.1.4 代码实践

**实践目标**：理解 bo 的「字节数 vs 元素数」与 map 的「主机视图」。

**操作步骤**：

1. 打开 [host.cpp:147](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L147)，确认 `xrt::bo` 第二参数是 `DDR_BUFFSIZE_I_BYTES`（字节）。
2. 顺着 [host.cpp:95-99](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L95-L99) 手算：若 `POINT_SIZE=4096`、`SSR=4`、`NITER=4`（见 `example.mk`），`DDR_BUFFSIZE_I_BYTES` 展开为多少字节。
3. 对照 [host.cpp:151](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L151) 的 `map<real_dtype*>()`，说明主机指针 `mm2s_bo_mapped` 指向的元素个数 = 字节数 / `sizeof(real_dtype)`。

**需要观察的现象**：map 返回的指针可以像普通数组一样用 `[ss]` 索引（[host.cpp:176](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L176)），没有任何「设备地址」的痕迹。

**预期结果**：你能用一张纸算出 bo 的字节数，并解释为什么 `map<real_dtype*>` 后元素数是字节数 / 4。

**待本地验证**：在真实环境跑 `make -f example.mk example_host`，加一行 `std::cout << DDR_BUFFSIZE_I_BYTES << " " << DDR_BUFFSIZE_O_BYTES;` 打印实际字节数核对。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `map<real_dtype*>()` 改成 `map<int16_t*>()`，主机侧元素个数会变成原来的几倍？读写语义会出错吗？

**答案**：元素个数变成 2 倍（因为 `int16_t` 占 2 字节而非 4）。语义上不会立刻崩溃——map 只决定主机指针的解释粒度——但你会把一个 32-bit 分量当成两个 16-bit 来读写，数值全错。map 的模板类型必须与数据真实位宽一致。

**练习 2**：为什么 `xrt::bo` 构造函数要传「字节数」而不是「元素个数」？

**答案**：因为 bo 是「设备内存的字节缓冲」抽象，与主机侧 C++ 类型无关；运行时只关心要分配多少字节。元素个数 → 字节数的换算（`N * sizeof(T)`）是调用方的责任，本例在编译期常量里就完成了。

---

### 4.2 XCL_BO_SYNC：数据跨边界的同步方向

#### 4.2.1 概念说明

`map` 给了主机一个指针，但这不代表「CPU 写的值内核立刻能看到」，也不代表「内核写的值 CPU 立刻能读到」。原因有二：

1. **物理隔离**：在 PCIe 卡（x86 主机 + Alveo）上，主机内存与设备内存是两块物理存储，map 的指针背后可能是一份主机侧影子缓冲（shadow buffer），真正进设备内存要靠 DMA 搬运。
2. **缓存一致性**：即便在嵌入式 Versal 上主机与 PL 共享 LPDDR，CPU 的缓存（cache）里可能还留着旧值，需要显式失效（invalidate）才能读到内核刚 DMA 写入的新值。

`sync` 就是显式触发「搬运 + 缓存维护」的接口，它带一个方向参数：

- `XCL_BO_SYNC_BO_TO_DEVICE`：主机 → 设备。把主机填好的数据搬进设备内存，内核启动前用。
- `XCL_BO_SYNC_BO_FROM_DEVICE`：设备 → 主机。让主机视图失效并读回内核写的结果，读取结果前用。

口诀：**写数据进设备用 TO_DEVICE，读结果出设备用 FROM_DEVICE**。

#### 4.2.2 核心流程

一个完整 bo 生命周期的典型同步序列（x86 PCIe 平台）：

```
bo = xrt::bo(...)                      # 申请设备缓冲
p  = bo.map<T*>()                      # 拿主机指针
for (...) p[i] = ...                   # 主机写输入
bo.sync(XCL_BO_SYNC_BO_TO_DEVICE)      # ★ 把输入搬进设备
run.set_arg(0, bo); run.start(); run.wait()   # 内核跑
bo.sync(XCL_BO_SYNC_BO_FROM_DEVICE)    # ★ 把结果搬回主机
for (...) ... = p[i]                   # 主机读结果
```

本例（嵌入式 Versal）省略了 TO_DEVICE 那一步——见 4.2.3 的解释。

#### 4.2.3 源码精读

内核跑完（`wait()` 返回）后，主机在读取 `s2mm_bo_mapped` 之前**先**做 FROM_DEVICE 同步：

[dsp/L2/examples/vss_fft_ifft_1d/host.cpp:212-216](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L212-L216) —— 对 `s2mm_bo`（结果缓冲）和 `mm2s_bo`（输入缓冲）都调用 `sync(XCL_BO_SYNC_BO_FROM_DEVICE)`。

**为什么读结果前必须 FROM_DEVICE？** 因为 `s2mm` 内核刚把计算结果 DMA 写进了这块设备内存（见 s2mm 的 `m_axi` 写回）。主机侧 `map` 指针背后（或缓存里）的还是旧值/未定义值，不同步直接读会读到脏数据。`FROM_DEVICE` 让主机视图失效并取回设备最新内容，这才保证了紧接着的逐点比对有意义：

[dsp/L2/examples/vss_fft_ifft_1d/host.cpp:235-244](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L235-L244) —— 在 sync 之后逐点读 `s2mm_bo_mapped[ss]`，与 `ref_output.txt` 的黄金值比对，误差超 `level = 1<<8` 即判失败。

**那为什么本例没有 TO_DEVICE？** 看 [host.cpp:172-182](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L172-L182)：主机填充完输入就直接 `set_arg`、`start`，中间没有 sync。原因是本例的编译目标是嵌入式 aarch64（见 `example.mk` 的 `example_host` 用 `aarch64-linux-gnu-g++`）。在嵌入式 Versal 上，bo 分配在 PS（处理器系统）与 PL 共享的 LPDDR 里，`map()` 返回的是**真正的零拷贝指针**——CPU 写即写进 LPDDR，PL 内核从同一片 LPDDR 读，无需 DMA 搬运，故省略 TO_DEVICE。

> 可移植性提示：如果同样这段 host.cpp 拿到 x86 PCIe 平台跑，主机与设备内存物理分离，map 背后是影子缓冲，**必须在填充后、start 前补一句 `mm2s_bo.sync(XCL_BO_SYNC_BO_TO_DEVICE)`**，否则内核读到的是空缓冲。本例省略它，是因为它的目标平台是嵌入式零拷贝。

[host.cpp:215](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L215) 对 `mm2s_bo`（输入缓冲）也做了一次 FROM_DEVICE，这在功能上是多余但无害的——它只是把「主机当初写的、又被内核读过的」数据再同步回来一遍。

#### 4.2.4 代码实践

**实践目标**：亲手在源码里定位两个方向的 sync，理解「读结果前必须 FROM_DEVICE」。

**操作步骤**：

1. 在 [host.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp) 里搜索 `XCL_BO_SYNC`，确认全文件只有两处、且都是 `FROM_DEVICE`。
2. 搜索 `XCL_BO_SYNC_BO_TO_DEVICE`，确认**没有**任何结果。
3. 解释：为什么填充输入（172-182）之后不 sync 到设备，程序仍能在 Versal 上跑通？

**需要观察的现象**：grep 结果显示零个 `TO_DEVICE`；两处 `FROM_DEVICE` 都在 `s2mm_run.wait()` / `mm2s_run.wait()` 之后、读取 `*_bo_mapped` 之前。

**预期结果**：你能说清「嵌入式零拷贝 → 可省 TO_DEVICE」与「读结果 → 必须 FROM_DEVICE」这两条规则。

**待本地验证**：若环境允许，把这份 host.cpp 改到 x86 PCIe 平台构建，会观察到不加 TO_DEVICE 时内核读到全零、结果错误。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 212 行 `s2mm_bo.sync(XCL_BO_SYNC_BO_FROM_DEVICE)` 删掉，程序会怎样？

**答案**：主机侧 `s2mm_bo_mapped` 不会反映内核刚写回的结果（缓存未失效 / 影子缓冲未读回），第 239-240 行读到的会是旧值或垃圾，逐点比对全部超阈值，最终打印 `*** FAILED ***`。

**练习 2**：`XCL_BO_SYNC_BO_TO_DEVICE` 与 `XCL_BO_SYNC_BO_FROM_DEVICE` 的「方向」是站在谁的视角说的？

**答案**：站在**主机**视角。「TO_DEVICE」= 数据从主机流向设备；「FROM_DEVICE」= 数据从设备流向主机。与 DMA 通道的命名（H2C / C2H）一致：TO_DEVICE 对应 H2C，FROM_DEVICE 对应 C2H。

---

### 4.3 group_id：把缓冲挂到正确的存储端口

#### 4.3.1 概念说明

回到那个被我们回避的问题：`xrt::bo(device, size, group_id)` 的第三个参数 `group_id` 到底是什么？

内核通过 `m_axi` 端口访问设备内存（DDR/HBM/LPDDR）。一个内核可能有多个 `m_axi` 端口，每个端口可以被设计者绑到不同的存储 bank。XRT 给「内核第 N 个参数所对应的存储端口组」编一个号，就叫 **group_id**。创建 bo 时传入某个 group_id，就是告诉运行时：「把这块缓冲分配到这个端口组能直接访问的那片存储上」。

为什么必须对齐？因为内核的 `m_axi` 端口在硬件上只连到了特定 bank。如果 bo 被分到了内核端口够不着的另一片存储，内核读到的是垃圾或超时。**group_id 的作用，就是保证 bo 与内核端口落在同一片物理存储上。**

#### 4.3.2 核心流程

```
内核签名:  void kernel(T* mem, ...)              # mem 是 m_axi 参数（指针），排在第 0 个参数
HLS:       #pragma HLS interface m_axi port=mem   # 声明 mem 为内存映射端口
链接:       system.cfg 里 sp=kernel.mem:LPDDR     # 把该端口绑到 LPDDR bank
主机:       gid = kernel.group_id(0)              # 查「第 0 个参数」对应的端口组号
           bo  = xrt::bo(device, size, gid)       # bo 落在该端口组对应的存储上
           run.set_arg(0, bo)                     # 把 bo 绑回第 0 个参数 → 内核的 mem 指向 bo
```

关键约定：`group_id(arg)` 里的数字是**内核签名里参数的下标**（从 0 开始），且只对 `m_axi`（指针）参数有意义。本例 `mm2s_wrapper` / `s2mm_wrapper` 的第 0 个参数 `mem` 正是 `m_axi`，故用 `group_id(0)`；而 `set_arg(0, bo)` 用同一个 0，三者必须一致。

#### 4.3.3 源码精读

先看内核签名，确认第 0 个参数就是内存指针。`mm2s_wrapper` 把 DDR 数据读出、打成 AXI 流喂给 AIE：

[dsp/L1/tests/hw/mm2s/mm2s.cpp:143-148](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp#L143-L148) —— `mm2s_wrapper(TT_DATA mem[...], TT_STREAM sig_o[...])`，`mem` 是参数 0，`#pragma HLS interface m_axi port=mem bundle=gmem` 声明它是内存映射端口；`sig_o` 是参数 1，声明为 `axis`（AXI 流，不走 DDR）。

`s2mm_wrapper` 对称——把 AIE 吐出的流写回 DDR：

[dsp/L1/tests/hw/s2mm/s2mm.cpp:120-127](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/s2mm/s2mm.cpp#L120-L127) —— 同样 `mem` 为参数 0、`m_axi`，`sig_i` 为参数 1、`axis`。

因为两个内核的内存端口都绑到了 `LPDDR`，主机创建两个 bo 时都传 `group_id(0)`：

[dsp/L2/examples/vss_fft_ifft_1d/host.cpp:147-149](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L147-L149) —— `xrt::bo(my_device, DDR_BUFFSIZE_I_BYTES, mm2s.group_id(0))`；输出行还会把 `group_id(0)` 的实际数值打印出来，便于核对。

[dsp/L2/examples/vss_fft_ifft_1d/host.cpp:154-157](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L154-L157) —— `s2mm_bo` 同理用 `s2mm.group_id(0)`。

那 `group_id(0)` 这个号从哪来？从链接期配置来——`system.cfg` 里的 `sp=` 行把端口绑到具体存储：

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L14) —— `sp=mm2s.mem:LPDDR`：把实例 `mm2s` 的 `mem` 端口绑到 `LPDDR` 存储组。

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:33](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L33) —— `sp=s2mm.mem:LPDDR`：`s2mm` 的 `mem` 端口也绑到 `LPDDR`。

于是闭环成立：`sp=` 在硬件上把端口钉到 LPDDR → `group_id(0)` 在运行时返回 LPDDR 的组号 → bo 落在 LPDDR → `set_arg(0, bo)` 让内核 `mem` 指向 bo → 内核经 `m_axi` 端口读写 LPDDR。任何一环错位（例如 bo 分到别的 bank），内核都会访问不到正确数据。

> 名词补充：`system.cfg` 里 `nk=` 是「实例化」（kernel 名:数量:实例名），`sp=` 是「端口绑存储」（实例.端口:bank），`sc=` 是「流互连」（源.端口:目的.端口）。本讲的 `sp=` 属于第二种。

#### 4.3.4 代码实践

**实践目标**：把「内核参数下标 ↔ group_id ↔ sp= 绑定」三者的对应关系读通。

**操作步骤**：

1. 打开 [mm2s.cpp:143-144](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp#L143-L144)，确认 `mem` 是参数 0 且为 `m_axi`。
2. 打开 [system.cfg:14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L14)，确认 `sp=mm2s.mem:LPDDR`。
3. 打开 [host.cpp:147](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L147) 与 [host.cpp:190](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L190)，确认 `group_id(0)` 与 `set_arg(0, ...)` 用的是同一个下标 `0`。

**需要观察的现象**：三个文件里的下标 `0`、端口名 `mem`、bank 名 `LPDDR` 完全咬合，构成一条闭环。

**预期结果**：你能用一句话说清——`group_id(0)` 决定了 bo 挂到 `mm2s.mem` 端口所绑定的那片存储（本例即 LPDDR）。

**待本地验证**：在 hw_emu 跑通后，把 [host.cpp:148](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L148) 打印的 `group_id(0)` 数值记下来；再对照平台 `platforminfo` 里 LPDDR 的内存组号，确认两者一致。

#### 4.3.5 小练习与答案

**练习 1**：如果内核有两个 `m_axi` 指针参数 `a`、`b`（分别在签名第 0、1 位），想让它们访问不同 bank，主机代码该怎么写？

**答案**：分别用 `xrt::bo(dev, sizeA, kernel.group_id(0))` 与 `xrt::bo(dev, sizeB, kernel.group_id(1))` 创建两个 bo，再 `set_arg(0, boA)`、`set_arg(1, boB)`；并在 `system.cfg` 里写 `sp=kernel.a:DDR[0]` 与 `sp=kernel.b:DDR[1]` 把两个端口绑到不同 bank。

**练习 2**：本例 `mm2s` 和 `s2mm` 的 `group_id(0)` 返回的号相同吗？为什么？

**答案**：相同——两者都被 `sp=` 绑到了同一个 `LPDDR` 存储组，所以运行时返回的组号一致。这也意味着本例输入、输出缓冲共用了同一片 LPDDR（带宽上限受单一端口约束，见 4.4）。

---

### 4.4 DDR / HBM 多 bank 分区与带宽

#### 4.4.1 概念说明

理解了 group_id，就能谈带宽。本例把 mm2s（读）和 s2mm（写）都绑到同一片 `LPDDR`——功能正确，但所有访存挤在一个端口上，带宽就是这个端口的上限。这在带宽需求低的例子里够用，但真实 DSP/视觉内核动辄要几十 GB/s，单 bank 会成为瓶颈。

板卡提供多片存储来破这个上限：

| 典型平台 | 存储资源 | 特点 |
| --- | --- | --- |
| Alveo U50 | 1× DDR4 + 1× HBM2（32 个逻辑通道） | HBM 带宽极高（~460 GB/s）但单通道窄 |
| Alveo U280 | 2× DDR4 + 2× HBM2 | DDR 与 HBM 可分摊不同数据流 |
| Versal VCK190（嵌入式） | LPDDR4（PS/PL 共享） | 零拷贝，但通常单组 |

把不同内核、或同一内核的不同端口绑到不同 bank，让访存并行起来，这就是**存储分区（banking）**。理论聚合带宽近似线性增长：

\[
B_{\text{agg}} \;\approx\; \min\!\big(N_{\text{bank}} \cdot B_{\text{bank}},\; B_{\text{总线上限}}\big)
\]

其中 \(N_{\text{bank}}\) 是被均衡使用的 bank 数，\(B_{\text{bank}}\) 是单 bank 峰值带宽。前提是各 bank 的访问量大致均衡——若所有流量仍涌向一个 bank，分区就白做了。

#### 4.4.2 核心流程

```
设计期:  为内核声明多个 m_axi 端口（或多个内核各一个端口）
链接期:  system.cfg 用 sp= 把不同端口绑到不同 bank
                sp=mm2s.mem:DDR[0]
                sp=s2mm.mem:DDR[1]      # 读写分流到两片 DDR
主机期:  为每个端口用对应 group_id 建独立 bo
                bo_in  = xrt::bo(dev, sz_in,  mm2s.group_id(0))
                bo_out = xrt::bo(dev, sz_out, s2mm.group_id(0))
        → 读写在两片物理存储上并行，带宽翻倍
```

#### 4.4.3 源码精读

本例是「单 bank」的反例——两个端口都指向 LPDDR，便于教学，但牺牲了带宽分流的可能：

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:13-14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L13-L14) 与 [system.cfg:33](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L33) —— `sp=mm2s.mem:LPDDR` 和 `sp=s2mm.mem:LPDDR`，两者共用 LPDDR，没有分区。本例把带宽压力转嫁给了 AIE 阵列本身，而非访存端口，故单 bank 足够。

流互连部分则展示了「PL↔AIE」的连接——mm2s 的 4 条输出流直连前转置内核，后转置内核再直连 s2mm 的 4 条输入流：

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:23-31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L23-L31) —— `sc=mm2s.sig_o_*:vss_fft_ifft_1d_front_transpose.sig_i_*` 与 `sc=vss_fft_ifft_1d_back_transpose.sig_o_*:s2mm.sig_i_*`。这些 `sc=` 是 AXI 流互连，不经过 DDR，所以不占 DDR 带宽——这也是把访存压力降到单 LPDDR 仍能跑通的原因。

> 延伸：多 DDR 分区的真实样例在 `utils/L1/tests` 下，如 `cache_ro_1DDR_with_e` 与 `cache_ro_2DDR_with_e`（u12-l2 会精读），后者把只读缓存分别挂到两片 DDR 以翻倍读带宽。本讲只需建立「sp= 决定 bank、bank 决定带宽」的心智模型。

#### 4.4.4 代码实践

**实践目标**：学会从 `system.cfg` 的 `sp=` 行读出带宽拓扑，并设想分区改造。

**操作步骤**：

1. 读 [system.cfg:14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L14) 与 [system.cfg:33](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L33)，确认本例把 mm2s 与 s2mm 绑到同一个 `LPDDR`。
2. 假设目标平台有 `DDR[0]` 和 `DDR[1]` 两片独立 DDR，写出能让读、写分流的改造方案：把 `sp=mm2s.mem:LPDDR` 改成 `sp=mm2s.mem:DDR[0]`，把 `sp=s2mm.mem:LPDDR` 改成 `sp=s2mm.mem:DDR[1]`。
3. 说明改造后主机侧 bo 创建代码**是否需要改动**（提示：主机用的是 `mm2s.group_id(0)` / `s2mm.group_id(0)`，会自动跟随新的 sp 绑定）。

**需要观察的现象**：`sc=` 流互连行与 `sp=` 存储绑定行分开列在配置里；改 `sp=` 的 bank 名不需要动主机 C++ 代码。

**预期结果**：你能解释——改 bank 绑定后，`group_id(0)` 返回的组号自动更新，bo 自动落到新 bank，主机代码因「按 group_id 取号」而保持不变；这正是用 `group_id(0)` 而非硬编码 bank 号的好处。

**待本地验证**：在有双 DDR 的平台上实际改 `sp=` 并对比 profile 报告里的 DDR 读写带宽（需开 `make ... PROFILE=yes`，见 `utils.mk` 的 `--profile_kernel`）。

#### 4.4.5 小练习与答案

**练习 1**：为什么把 mm2s 和 s2mm 绑到两片不同 DDR 通常比绑到同一片更好？

**答案**：读（mm2s）和写（s2mm）走不同的物理端口与存储芯片，访存可并行，理论带宽近似翻倍（\(B_{\text{agg}} \approx 2 B_{\text{bank}}\)）。若共用一片，两者竞争同一端口的带宽，还会因读写交替降低有效吞吐。

**练习 2**：HBM2 有 32 个逻辑通道，是否意味着随便绑都能拿到 32 倍带宽？

**答案**：否。带宽提升的前提是各通道访问**均衡**。若所有数据流仍涌向一个通道，其余 31 个空闲，聚合带宽仍是单通道水平。分区要配合内核的数据划分（如 SSR 并行）才能真正摊开流量。

## 5. 综合实践

把本讲四个模块串起来，做一次「bo 全生命周期」的源码追踪，目标是合上从「申请缓冲」到「读到正确结果」的完整环。

**任务**：以 `dsp/L2/examples/vss_fft_ifft_1d` 为对象，画一张时序图，横轴是时间，纵轴分四条泳道——**主机 CPU**、**mm2s_bo（输入缓冲）**、**PL/AIE 计算**、**s2mm_bo（输出缓冲）**。

1. 在主机泳道上，按顺序标出以下事件及其 [host.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp) 行号：
   - `xrt::bo(... group_id(0))` 创建两个 bo（147、154）
   - `map<real_dtype*>()` 拿主机指针（151、159）
   - 填充输入（172-182）
   - `set_arg(0, bo)`（190、193）
   - `start()` / `wait()`（196-206）
   - `sync(FROM_DEVICE)`（212、215）
   - 读结果比对（235-244）
2. 在 mm2s_bo 泳道，标注「数据流向」：主机写入 →（嵌入式零拷贝，省略 TO_DEVICE）→ mm2s 内核经 `m_axi` 读取。
3. 在 s2mm_bo 泳道，标注「数据流向」：s2mm 内核经 `m_axi` 写入 → FROM_DEVICE 同步 → 主机读取。
4. 用箭头把 `group_id(0)`（host.cpp:147）↔ `sp=mm2s.mem:LPDDR`（system.cfg:14）↔ `m_axi port=mem`（mm2s.cpp:144）三者连起来，体现 4.3 的闭环。

**验收**：你能看着这张图，向别人解释清楚三件事——(a) 为什么读结果前必须有 FROM_DEVICE；(b) group_id(0) 是怎么和 system.cfg 的 sp= 对上的；(c) 本例为什么不需要 TO_DEVICE。这正对应本讲的三个核心学习目标。

**待本地验证**：若环境就绪，跑 `make -f example.mk all PLATFORM=<versal平台> DSPLIB_ROOT_DIR=<dsp根>`，在 hw_emu 输出里核对 host.cpp 各 `PASSED:` 行的顺序与你的时序图一致。

## 6. 本讲小结

- `xrt::bo` 是设备内存缓冲的句柄；`map<T*>()` 给主机一个可读写指针，但 map 本身不搬运数据。
- `sync` 负责跨主机/设备边界的数据搬运与缓存一致性：`TO_DEVICE` 在内核启动前把输入搬进设备，`FROM_DEVICE` 在读结果前把设备写回的数据取回主机。
- 本例省略 `TO_DEVICE`，是因为它编译目标为嵌入式 Versal，map 是 PS/PL 共享 LPDDR 的零拷贝指针；换到 x86 PCIe 平台必须补上。
- `group_id(arg)` 返回内核第 `arg` 个（`m_axi`）参数对应的存储端口组号；创建 bo 时传入它，保证 bo 落在内核端口能访问的 bank 上；它与 `set_arg(arg, bo)` 的下标、内核签名里的参数位置三者必须一致。
- bank 由 `system.cfg` 的 `sp=实例.端口:bank` 在链接期决定；本例把 mm2s 与 s2mm 都绑到单一 `LPDDR`，是单 bank 反例。
- 多 bank 分区能让访存并行、带宽近似线性增长（\(B_{\text{agg}} \approx N_{\text{bank}} \cdot B_{\text{bank}}\)），前提是各 bank 流量均衡。

## 7. 下一步学习建议

本讲把主机侧的「缓冲与访存」讲透了。接下来有两条路：

- **向纵深走（数据流与性能）**：去看 u12-l1「dataflow、SSR、datawidth 与 II 调优」和 u12-l2「资源/时序：URAM、HBM/DDR 分区与报告」——后者会精读 `utils/L1/tests/cache_ro_1DDR_with_e` vs `cache_ro_2DDR_with_e`，给出真实的多 DDR 分区样例与综合报告解读。
- **向系统走（PL↔AIE 桥接）**：去看 u13-l1「ADF 图、窗口/流与 PL↔AIE 边界」——本讲的 mm2s/s2mm 正是 PL↔AIE 边界的搬运器，下一讲会展开 AIE 图（`xrt::graph`）如何消费这些流，以及 `sc=` 流互连如何替代 DDR 搬运。

建议先读 u12-l2 把「多 bank → 带宽」的定量关系夯实，再读 u13-l1 把整条 PL↔AIE 数据通路补全。
