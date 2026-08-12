# DataCopy 搬运与 LocalMemAllocator 自主内存管理

## 1. 本讲目标

上一讲（u3-l2）我们把 `GlobalTensor`/`LocalTensor` 这两个描述符拆开了，知道了它们「装了什么、怎么构造」。但留下两个黑盒：数据**怎么从 GM 走到 UB**（u3-l2 里的 `DataCopy` 只当它是一句搬运），以及那块 UB 缓冲**到底由谁、用什么规则切出来**（u3-l2 只点名了 `LocalMemAllocator`，没讲它的分配机制）。

本讲打开这两个黑盒，回答三个问题：

1. **搬运**：`DataCopy` 接口长什么样？怎么判断一次搬运是 GM→UB 还是 UB→GM？它走哪条硬件流水线？
2. **分配**：`LocalMemAllocator` 是怎样一个分配器？它的 `Alloc` 怎么切内存、有哪些重载和约束？
3. **范式**：「用 `LocalMemAllocator` 自主管内存」这套编程范式和「用 `TPipe/TQue` 框架管内存」有什么本质区别？为什么自主范式里**到处都要写 `PipeBarrier`**？

学完后你应当能够：读懂 add 样例里每一行 `DataCopy`/`Alloc`/`PipeBarrier` 的含义；独立用 `LocalMemAllocator` 申请 UB、用 `DataCopy` 完成一轮「GM→UB→计算→UB→GM」的数据搬运；并能说清「自主管理内存 = 自己管分配 + 自己管同步」这一取舍。

## 2. 前置知识

本讲默认你已掌握 u3-l1（内存层级与地址空间限定符）和 u3-l2（GlobalTensor/LocalTensor）。重点回顾四条结论：

- **多级存储与两条通路**：Vector 通路只接 UB，搬运走 `GM ↔ UB`；搬运用的硬件单元叫 **MTE**（Memory Transfer Engine），其中 MTE2 负责「外部→片上」（GM→UB），MTE3 负责「片上→外部」（UB→GM）。
- **Tensor 是描述符**：`GlobalTensor` 绑定已存在的 GM 指针、不分配内存；`LocalTensor` 描述一段已分配的片上缓冲。
- **两套命名 + GetPhyType 桥接**：硬件层 `Hardware`（如 `Hardware::UB`），逻辑层 `TPosition`（如 `TPosition::VECCALC`），靠 `GetPhyType` 互转。
- **批量 vs 标量**：`DataCopy` 是批量搬运（吞吐高），`GetValue`/`SetValue` 是标量访问（吞吐低）。本讲只讲批量这条主路。

一个关键直觉先建立起来：

> **算子的数据搬运不是「一行函数调用」那么简单——它跨了两条独立的硬件流水线**：搬运用 MTE2/MTE3，计算用 Vector（V）。这两条流水线可以并行、乱序执行，所以「搬完才能算、算完才能搬回」这个先后顺序，必须由**同步原语**显式保证。理解了这一点，你才会明白为什么 add 样例里穿插了那么多 `PipeBarrier`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [include/basic_api/kernel_operator_data_copy_intf.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_data_copy_intf.h) | `DataCopy` 系列搬运接口的**声明**（公开头文件，本讲主战场之一），按 Level 0/1/2 分级 |
| [include/basic_api/kernel_tensor.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h) | `LocalMemAllocator` 类的**声明**（同 u3-l2，本讲重点读它的 `Alloc` 重载与 `head_` 字段） |
| [impl/basic_api/kernel_tensor_impl.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h) | `LocalMemAllocator` 的**实现**（内部头文件，看分配器到底怎么切内存） |
| [impl/basic_api/kernel_event.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_event.h) | `GetDefaultPosition`/`GetPhyType` 的实现（两套命名的桥接函数，本讲用来解释 `Alloc` 的位置推断） |
| [include/basic_api/kernel_operator_block_sync_intf.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_block_sync_intf.h) | `PipeBarrier` 等同步原语的声明（本讲只用到最简单的 `PipeBarrier<PIPE_ALL>()`） |
| [examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc) | 矢量加法样例，本讲所有源码精读与综合实践的对照基准 |

> 提示：`impl/` 下是内部头文件，只读不 include；写算子时永远 include 公开的 `kernel_operator.h`（它已聚合了下述所有接口）。

## 4. 核心概念与源码讲解

### 4.1 DataCopy 搬运接口

#### 4.1.1 概念说明

`DataCopy` 是 Ascend C 里**最常用的批量搬运接口**，专门负责 Tensor 之间的大块数据复制。它的核心特征有三条：

1. **第一个参数永远是目的（dst），第二个永远是源（src）**。这条规则适用于所有重载——记住「先 dst 后 src」，就不会把搬运方向搞反。
2. **方向由参数类型隐式决定**：`DataCopy(LocalTensor, GlobalTensor, ...)` 是 GM→UB（加载），`DataCopy(GlobalTensor, LocalTensor, ...)` 是 UB→GM（回写）。换句话说，看哪一边是 `GlobalTensor` 就知道 GM 在哪头。
3. **方向决定流水线**：GM→UB 走 MTE2，UB→GM 走 MTE3。这个映射写死在声明里的 `__inout_pipe__` 标注中（见 4.1.3），是理解同步需求的关键。

`DataCopy` 按「控制粒度」分了三个 Level（和 u3-l2 的标量/批量分层是同一套思路）：

| Level | 参数形态 | 适合场景 |
|---|---|---|
| **Level 2** | `DataCopy(dst, src, count)` —— 只给元素个数 | 最简单，连续整块搬运，**初学首选** |
| **Level 1** | `DataCopy(dst, src, SliceInfo[], ...)` —— 用切片描述多维搬运 | 需要按维度跨步搬运（如取矩阵的若干行） |
| **Level 0** | `DataCopy(dst, src, DataCopyParams{blockCount, blockLen, srcGap, dstGap})` —— 直接控 block/repeat/gap | 极致性能，手动控制底层搬运单元的每一次 repeat |

此外还有处理「尾部不对齐」的 `DataCopyPad`、做格式转换的 `Nd2Nz`/`Nz2Nd` 等增强重载。本讲聚焦最常用的 Level 2 count 模式，其余留作 4.1.5 的练习和后续进阶讲义。

#### 4.1.2 核心流程

一次「GM→UB→计算→UB→GM」的标准搬运流程（add 样例就是它）：

```text
1. GM→UB（加载）   DataCopy(xLocal, xGm, blockLength);   // MTE2 管线
2. 同步            PipeBarrier<PIPE_ALL>();              // 等 MTE2 写完
3. 计算            Add(zLocal, xLocal, yLocal, blockLength); // V 矢量管线
4. 同步            PipeBarrier<PIPE_ALL>();              // 等 V 算完
5. UB→GM（回写）   DataCopy(zGm, zLocal, blockLength);   // MTE3 管线
6. 同步            PipeBarrier<PIPE_ALL>();              // 等 MTE3 写完
```

每一步搬运/计算都落在一条**独立的硬件流水线**上：MTE2、V、MTE3 三条线彼此独立、可并行。如果不插 `PipeBarrier`，第 3 步的 `Add` 可能在第 1 步 `DataCopy` 还没把数据写进 UB 时就去读 `xLocal`，读到脏数据。所以「跨流水线」处必须同步——这是本讲反复强调的核心约束。

> 名词解释：**流水线（pipe）** 是 AI Core 里一组可独立调度的硬件执行单元。常见的有 `MTE2`（外部→片上搬运）、`MTE3`（片上→外部搬运）、`MTE1`（片上 L1 搬运，Cube 通路用）、`M`/`V`（矢量计算）、`S`（标量）、`FIX`（Cube 结果回写）等。`PIPE_ALL` 表示「等待所有流水线」，是一个保守的全屏障。

#### 4.1.3 源码精读

先看 add 样例里真实用到的两行（[add.asc:42-43](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L42-L43)）：

```cpp
AscendC::DataCopy(xLocal, xGm, blockLength);   // GM → UB：dst 是 LocalTensor，src 是 GlobalTensor
AscendC::DataCopy(yLocal, yGm, blockLength);
```

它们匹配的是 **Level 2 count 模式的「GM→UB」重载**，声明在 [kernel_operator_data_copy_intf.h:223-232](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_data_copy_intf.h#L223-L232)：

```cpp
/*
 * @ingroup DataCopy Level 2
 * @brief datacopy from src to dst, applicable to vector data
 * @param [out] dst output LocalTensor
 * @param [in]  src input GlobalTensor
 * @param [in]  count Number of operands
 */
template <typename T>
__aicore__ inline __inout_pipe__(MTE2) void DataCopy(
    const LocalTensor<T>& dst, const GlobalTensor<T>& src, const uint32_t count);
```

注意两个细节：

- **`__inout_pipe__(MTE2)`**：这个标注明确告诉我们「这次搬运走 MTE2 流水线」。它是「GM→UB 走 MTE2」这一结论的**直接源码出处**。
- **`count` 是 `uint32_t`，单位是「元素个数」而非字节**。`blockLength` 在 add 里是 2048，所以一次搬 2048 个 float。

而回写那行 `DataCopy(zGm, zLocal, blockLength)`（[add.asc:61](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L61)）匹配的是**对称的「UB→GM」重载**，[kernel_operator_data_copy_intf.h:234-243](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_data_copy_intf.h#L234-L243)：

```cpp
template <typename T>
__aicore__ inline __inout_pipe__(MTE3) void DataCopy(
    const GlobalTensor<T>& dst, const LocalTensor<T>& src, const uint32_t count);
```

对比可见：**参数顺序整体翻转**（dst 从 LocalTensor 变成 GlobalTensor），`__inout_pipe__` 也从 `MTE2` 变成 `MTE3`。这就是 4.1.1 所说「方向由参数类型隐式决定、并决定流水线」的源码体现。

把这两条声明和 add 样例的三处 `PipeBarrier`（[add.asc:44](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L44)、[add.asc:47](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L47)、[add.asc:62](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L62)）对上号：

```cpp
AscendC::DataCopy(xLocal, xGm, blockLength);  // MTE2
AscendC::DataCopy(yLocal, yGm, blockLength);  // MTE2
AscendC::PipeBarrier<PIPE_ALL>();             // ① 屏障：等 MTE2 落 UB，再让 V 读
AscendC::Add(zLocal, xLocal, yLocal, blockLength);  // V
AscendC::PipeBarrier<PIPE_ALL>();             // ② 屏障：等 V 算完，再让 MTE3 读
AscendC::DataCopy(zGm, zLocal, blockLength);  // MTE3
AscendC::PipeBarrier<PIPE_ALL>();             // ③ 屏障：等 MTE3 落 GM（Kernel 结束前的保险）
```

三处屏障分别保护了「MTE2→V」「V→MTE3」「MTE3→（核结束）」三处跨流水线边界。删掉任何一处都可能引入数据竞争——这也是 u7-l1（同步机制）要展开的主题，本讲先建立这个直觉。

> 关于对齐：`DataCopy`（含 Level 2）通常要求搬运的数据量是 **32 字节的整数倍**（32 字节 = 1 个 UB block = 8 个 float）。add 里 `blockLength=2048` 个 float = 8192 字节，显然满足。若总长不是 32 字节倍数（有零头），Level 2 的 `DataCopy` 处理不了零头，需要改用 `DataCopyPad`——它能在搬运时自动补齐（pad）尾部，见 [kernel_operator_data_copy_intf.h:381-389](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_data_copy_intf.h#L381-L389)。这正是 Tiling（u6-l2）要保证每块都对齐的原因之一。

#### 4.1.4 代码实践

**实践目标**：亲手验证「方向由参数顺序决定」，并体会搬运方向与流水线的对应。

**操作步骤**（源码阅读型 + 小改造）：

1. 打开 [add.asc:42-43](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L42-L43)，确认两行加载的参数顺序都是「LocalTensor 在前、GlobalTensor 在后」。
2. 对照 [kernel_operator_data_copy_intf.h:230-232](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_data_copy_intf.h#L230-L232) 的声明，确认它匹配的是 `__inout_pipe__(MTE2)` 那条重载。
3. 在脑海里把第 42 行写成反向：`DataCopy(xGm, xLocal, blockLength)`（GlobalTensor 在前）。此时它匹配的应是 [kernel_operator_data_copy_intf.h:241-243](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_data_copy_intf.h#L241-L243) 的 `MTE3` 重载，语义从「加载」变成「回写」。

**需要观察的现象**：仅交换两个参数的位置，搬运方向和所用流水线就完全互换——这印证了「方向隐式由参数类型决定」。

**预期结果**：你能不查文档，仅凭「哪个参数是 GlobalTensor」一眼判断任意一次 `DataCopy` 的方向与流水线。

> 待本地验证：在 CPU 调试模式下，把 add 的 `DataCopy(zGm, zLocal, blockLength)` 暂时改成 `DataCopy(zLocal, zGm, blockLength)`（方向写反），编译应能过（类型匹配 MTE2 重载），但运行结果会错——体会「类型系统能保证合法，但不保证语义正确」。

#### 4.1.5 小练习与答案

**练习 1**：add 样例里 `DataCopy(xLocal, xGm, blockLength)` 的第三个参数 `blockLength` 是字节数还是元素个数？依据是什么？

**答案**：是**元素个数**。依据是 [kernel_operator_data_copy_intf.h:228-229](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_data_copy_intf.h#L228-L229) 的注释 `@param count Number of operands`（操作数个数）。add 里 `blockLength=2048`，搬的是 2048 个 float，而非 2048 字节。

**练习 2**：为什么 Level 2 的 `DataCopy` 处理不了「总长 2050 个 float」的输入？

**答案**：因为 2050 个 float = 8200 字节，不是 32 字节（一个 UB block）的整数倍，尾部有 8 个 float 的零头。Level 2 `DataCopy` 要求 32 字节对齐，零头会被截断或越界。正确做法是用 `DataCopyPad`（[kernel_operator_data_copy_intf.h:381-389](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_data_copy_intf.h#L381-L389)）搬运零头并自动补齐，或在 Tiling 时把每块切到 32 字节的整数倍。

---

### 4.2 LocalMemAllocator

#### 4.2.1 概念说明

`LocalMemAllocator` 是基础 API 下**分配片上 LocalTensor 内存**的工具。上一讲（u3-l2）已经提到它是个「栈式 bump allocator」，本讲把它的内部机制讲透。

它解决的问题是：每核独享的 UB 是一块**有限且连续**的片上内存，算子里需要好几块缓冲（输入、输出、中间结果），怎么从这块大内存里切出若干小段，分配给各个 `LocalTensor`？

`LocalMemAllocator` 的答案极简——维护一个**游标 `head_`**，每次 `Alloc` 就在 `head_` 处切出请求大小的字节，再把 `head_` 往前推。它的特点：

1. **线性分配、不回收**：像「栈」一样只进不出，分配过的内存在该分配器生命周期内不复用（Kernel 结束时整块 UB 一起释放）。
2. **模板参数选硬件位置**：`LocalMemAllocator<Hardware::UB>` 分配 UB 内存，也可以是 `L1`/`L0A` 等。
3. **强约束**：构造时校验「硬件位置合法（不能是 GM/MAX）」，分配时校验「逻辑位置与硬件位置匹配」。

#### 4.2.2 核心流程

一个 `LocalMemAllocator` 的生命周期是「构造（定起点）→ 多次 Alloc（连续切分）→ 随核结束销毁」：

```text
构造   LocalMemAllocator<Hardware::UB> ubAllocator;
       └─ head_ 初始化为 UB 动态区起点（GetDynamicMemStartPos<UB>()）

Alloc  xLocal = ubAllocator.Alloc<float, blockLength>();   // 在 head_ 处切 N 字节，head_ += N
Alloc  yLocal = ubAllocator.Alloc<float, blockLength>();   // 紧接 xLocal 之后切
Alloc  zLocal = ubAllocator.Alloc<float, blockLength>();   // 紧接 yLocal 之后切
       └─ 三块缓冲在 UB 里首尾相接、互不重叠
```

每次 `Alloc` 切出的字节数为（`SizeOfBits<T>` 是元素位宽）：

\[
\text{bytes} = \frac{\text{SizeOfBits<T>} \times \text{tileSize}}{8}
\]

对 float（32 位）× 2048 = 8192 字节 = 8 KB。add 样例三块缓冲共占 \(3 \times 8\,\text{KB} = 24\,\text{KB}\)，远小于每核 UB 容量，所以安全。

#### 4.2.3 源码精读

先看类的声明（[kernel_tensor.h:312-333](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L312-L333)），它把「一个分配器 = 一个游标」这一本质写得明明白白：

```cpp
template <Hardware hard = Hardware::UB>   // 默认分配 UB
class LocalMemAllocator {
public:
    __aicore__ inline LocalMemAllocator();
    __aicore__ inline uint32_t GetCurAddr() const;                 // 查当前游标位置
    template <TPosition pos, class DataType, uint32_t tileSize>
    __aicore__ inline LocalTensor<DataType> Alloc();               // 显式指定逻辑位置 + 编译期大小
    template <class DataType, uint32_t tileSize>
    __aicore__ inline LocalTensor<DataType> Alloc();               // 只给类型+大小，位置由 hard 推断 ★add 用这个
    template <class DataType>
    __aicore__ inline LocalTensor<DataType> Alloc(uint32_t tileSize); // 运行期大小
    /* …还有 TensorTrait / Layout 等重载… */
private:
    uint32_t head_ = 0;                                              // 唯一的游标
};
```

add 样例用的是标 ★ 的那个重载 `Alloc<float, blockLength>()`（[add.asc:38-40](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L38-L40)）。

再看构造函数的实现（[kernel_tensor_impl.h:1734-1747](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1734-L1747)），它定了起点并加了约束：

```cpp
template <Hardware hard>
__aicore__ inline LocalMemAllocator<hard>::LocalMemAllocator()
{
    static_assert((hard != Hardware::GM) && (hard != Hardware::MAX),
                  "illegal hardware position GM or MAX");
    // CPU 调试模式还会断言：同一硬件位置同一时刻只能有一个 LocalMemAllocator
    if constexpr (hard == Hardware::UB) {
        head_ = GetDynamicMemStartPos<hard>();   // 跳过保留区，从动态区起点开始
    }
}
```

两个要点：

1. **`static_assert(hard != GM && hard != MAX)`**：分配器只管「片上本地」内存。GM 由 Host 的 ACL 管（u3-l2 已强调 LocalTensor 析构不 aclrtFree），所以禁止对 GM 用分配器。
2. **`head_ = GetDynamicMemStartPos<UB>()`**：UB 里有部分区域被系统保留（reserved），动态分配从保留区之后开始。`head_` 初值就是这个安全起点。

接着看 add 用的那个 `Alloc` 重载的实现（[kernel_tensor_impl.h:1766-1774](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1766-L1774)）：

```cpp
template <Hardware hard>
template <class DataType, uint32_t tileSize>
__aicore__ inline LocalTensor<DataType> LocalMemAllocator<hard>::Alloc()
{
    static_assert(!is_tensorTrait_v<DataType>, "currently not support TensorTrait type!");
    LocalTensor<DataType> output(GetDefaultPosition(hard), head_, tileSize); // ① 用 (pos, addr, size) 构造
    head_ += SizeOfBits<DataType>::value * tileSize / SizeOfBits<uint8_t>::value; // ② 游标前移
    return output;
}
```

两步正是 bump allocator 的全部逻辑：

- **① 构造 LocalTensor**：三个参数是 `(TPosition pos, uint32_t addr, uint32_t tileSize)`（即 u3-l2 讲过的构造函数）。这里 `pos` 由 `GetDefaultPosition(hard)` 推断——对 `Hardware::UB`，它返回 `TPosition::VECCALC`（[kernel_event.h:349-352](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_event.h#L349-L352)）。
- **② 游标前移**：`head_ += 位宽 × 元素数 / 8`（位转字节），下次 `Alloc` 就从新位置继续切。

那么 `GetDefaultPosition` 是什么？它是「硬件位置 → 默认逻辑位置」的桥接（[kernel_event.h:349-367](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_event.h#L349-L367)），核心映射：

```cpp
Hardware::UB   → TPosition::VECCALC
Hardware::L1   → TPosition::A1
Hardware::L0A  → TPosition::A2
Hardware::L0B  → TPosition::B2
Hardware::L0C  → TPosition::CO1
```

而它的逆函数 `GetPhyType`（[kernel_event.h:369](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_event.h#L369)）则在「显式指定 pos」的重载里用来校验一致性，例如（[kernel_tensor_impl.h:1755-1764](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1755-L1764)）：

```cpp
template <TPosition pos, class DataType, uint32_t tileSize>
LocalTensor<DataType> LocalMemAllocator<hard>::Alloc()
{
    static_assert(GetPhyType(pos) == hard, "logic pos and hardware pos not matched."); // ★
    LocalTensor<DataType> output(pos, head_, tileSize);
    head_ += SizeOfBits<DataType>::value * tileSize / SizeOfBits<uint8_t>::value;
    return output;
}
```

带 ★ 的 `static_assert` 正是 u3-l2 提到的「逻辑位置与硬件位置必须一致」的源码出处：你要在 `UB` 分配器上 `Alloc<TPosition::A1, ...>`（L1 的逻辑位置）是编译期错误。

最后是一张 `Alloc` 重载速查表（依据 [kernel_tensor.h:317-326](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L317-L326)）：

| 重载签名 | 位置来源 | 大小来源 | add 是否用 |
|---|---|---|---|
| `Alloc<pos, T, tileSize>()` | 显式 `pos`（校验 == hard） | 编译期 `tileSize` | 否 |
| `Alloc<T, tileSize>()` | `GetDefaultPosition(hard)` 推断 | 编译期 `tileSize` | **是**（`Alloc<float, blockLength>()`） |
| `Alloc<pos, T>(tileSize)` | 显式 `pos` | 运行期 `tileSize` | 否 |
| `Alloc<T>(tileSize)` | 推断 | 运行期 `tileSize` | 否 |
| `Alloc<T>()` / `Alloc(layout)` | TensorTrait/Layout | 由 Layout 决定 | 否 |

#### 4.2.4 代码实践

**实践目标**：动手算一遍 add 样例的 UB 占用，体会「栈式分配 + 容量约束」。

**操作步骤**（源码阅读型 + 手算）：

1. 读 [add.asc:37-40](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L37-L40)，注意三块缓冲**依次** `Alloc`，所以地址连续：`xLocal` 在 `head_0`，`yLocal` 在 `head_0 + 8KB`，`zLocal` 在 `head_0 + 16KB`。
2. 手算：`blockLength = 2048`（[add.asc:68](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L68)），每块 \(2048 \times 4\,\text{B} = 8\,\text{KB}\)，三块共 24 KB。
3. 查阅你的目标芯片每核 UB 容量（典型 192~256 KB 量级，随架构不同）。24 KB 远在容量内。
4. 思考边界：若把 `blockLength` 改大到 32768，三块共 \(3 \times 32768 \times 4 = 384\,\text{KB}\)，很可能超过 UB 容量——这正是 u6-l2「Tiling 分块」的根本动因。

**需要观察的现象**：`blockLength` 线性放大时，UB 占用线性增长；一旦超过容量，CPU 调试模式下 `CreateTensor` 会触发 buffer overflow 断言（u3-l2 已指出该断言位置）。

**预期结果**：你能说出「`LocalMemAllocator` 是线性切分、Kernel 内不复用；总占用受限于 UB 容量，所以大算子必须 Tiling 分块，每块单独一轮 GM↔UB 搬运」。

> 待本地验证：在 CPU 调试模式下把 `blockLength` 改成一个明显超容的值，编译运行应触发 buffer overflow 断言。

#### 4.2.5 小练习与答案

**练习 1**：add 样例用的是 `Alloc<float, blockLength>()`，没有写 `TPosition`。这块缓冲的逻辑位置（`logicPos`）最终被设成了什么？依据是哪一行代码？

**答案**：设成 `TPosition::VECCALC`。依据：这个重载用 `GetDefaultPosition(hard)` 推断位置（[kernel_tensor_impl.h:1771](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1771)），而 `GetDefaultPosition(Hardware::UB)` 返回 `TPosition::VECCALC`（[kernel_event.h:351-352](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_event.h#L351-L352)）。`VECCALC` 正是「Vector 计算用的 UB」的逻辑位置。

**练习 2**：为什么构造函数要 `static_assert(hard != Hardware::GM)`？如果允许对 GM 用分配器会发生什么？

**答案**：见 [kernel_tensor_impl.h:1737](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1737)。`LocalMemAllocator` 是「核内片上」分配器，靠移动 `head_` 游标切本核独享的 UB/L1。GM 是片外、全核共享、由 Host 的 ACL（`aclrtMalloc`/`aclrtFree`）管理的，Kernel 端的 bump 分配器既无权也无力管它。若强行允许，会与 Host 的 GM 所有权冲突，产生双重管理/内存泄漏。

**练习 3**：CPU 调试模式下，为什么「同一硬件位置同一时刻只能有一个 `LocalMemAllocator`」？

**答案**：见构造函数里的 `CheckAllocatorUsed` 断言（[kernel_tensor_impl.h:1739-1742](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1739-L1742)）。因为每个分配器各自维护一个 `head_` 游标，若同一块 UB 上同时存在两个分配器，它们的 `head_` 互不感知，会切出**重叠**的缓冲，造成数据踩踏。框架式 `TPipe` 通过统一管理缓冲池避免了这个问题（见 4.3）。

---

### 4.3 自主内存管理范式：自己管分配，也要自己管同步

#### 4.3.1 概念说明

前两模块分别讲了「搬运」和「分配」。本模块把它们合起来，讲清楚一种编程范式——**自主内存管理（autonomous）**，并预告它的对照面——**框架式内存管理（TPipe/TQue，u5-l1 详讲）**。

「自主」二字有两层含义，缺一不可：

1. **自主管分配**：用 `LocalMemAllocator` 手动 `Alloc` 每一块缓冲，自己决定大小、顺序、用几块。
2. **自主管同步**：搬运（MTE2/MTE3）与计算（V）跨流水线，必须**自己手动插 `PipeBarrier`** 保证先后顺序。

第二层是自主范式最易踩坑的地方，也是它和框架式的根本区别。框架式（`TQue`）用 `EnQue`/`DeQue` 的队列语义**自动**在流水线间插入同步（你只要按规矩入队出队，框架帮你管屏障）；自主式则把这个责任完全交给了开发者。

> 一句话对比：**自主式 = 灵活 + 繁琐（自己写屏障）；框架式 = 省心 + 受约束（按队列规矩来）**。

#### 4.3.2 核心流程

自主范式下一个「单输入、原地处理、单输出」算子的标准骨架：

```text
// —— 1. 描述 GM ——
GlobalTensor<float> xGm, zGm;
xGm.SetGlobalBuffer(x + block_idx*blockLength, blockLength);
zGm.SetGlobalBuffer(z + block_idx*blockLength, blockLength);

// —— 2. 自主分配 UB ——
LocalMemAllocator<Hardware::UB> ubAllocator;
LocalTensor<float> xLocal = ubAllocator.Alloc<float, blockLength>();

// —— 3. 搬入 + 同步 + 计算 + 同步 + 搬回 + 同步 ——
DataCopy(xLocal, xGm, blockLength);          // MTE2
PipeBarrier<PIPE_ALL>();                      // ★ 手动屏障：等 MTE2
/* 在 xLocal 上做矢量计算（Add/Muls/Exp …） */  // V
PipeBarrier<PIPE_ALL>();                      // ★ 手动屏障：等 V
DataCopy(zGm, xLocal, blockLength);           // MTE3
PipeBarrier<PIPE_ALL>();                      // ★ 手动屏障：等 MTE3
```

对照框架式（伪代码，u5-l1 详讲）：

```text
TPipe pipe;
TQue<VECIN, depth> inQ;  TQue<VECOUT, depth> outQ;
pipe.InitBuffer(inQ, ...); pipe.InitBuffer(outQ, ...);
auto xLocal = inQ.AllocTensor();   // 框架分配
DataCopy(xLocal, xGm, ...);
inQ.EnQue(xLocal);                 // 入队 → 框架自动插 MTE2→V 同步
auto xDeq = inQ.DeQue();           // 出队 → 同步点
/* 计算 */
outQ.EnQue(...);                   // 入队 → 框架自动插 V→MTE3 同步
...
```

差别一目了然：自主式里那些 `PipeBarrier` 是手写的；框架式里它们被 `EnQue`/`DeQue` 隐式包含了。

#### 4.3.3 源码精读

`PipeBarrier` 的声明极其简洁（[kernel_operator_block_sync_intf.h:48-49](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_block_sync_intf.h#L48-L49)）：

```cpp
template <pipe_t pipe>
__aicore__ inline void PipeBarrier();
```

它是一个**模板函数**，模板参数 `pipe` 指定要等哪条流水线。`PipeBarrier<PIPE_ALL>()` 表示「等到所有流水线都空闲」——最保守、最安全的屏障，初学用它绝不会错（add 样例三处都是它）。更精细的同步（只等特定两条流水线）用 `SetFlag`/`WaitFlag`（[kernel_operator_block_sync_intf.h:42-46](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_block_sync_intf.h#L42-L46)），那是 u7-l1 的主题。

现在回到 add 样例，把本讲三个模块的知识点一次性串起来读这个 Kernel（[add.asc:28-63](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L28-L63)）：

```cpp
template <uint32_t blockLength>
__vector__ __global__ void add_custom(__gm__ float* x, __gm__ float* y, __gm__ float* z)
{
    AscendC::InitSocState();                                 // 初始化核状态（u2-l1 提过）

    AscendC::GlobalTensor<float> xGm, yGm, zGm;              // ① 描述 GM（u3-l2）
    xGm.SetGlobalBuffer(x + block_idx * blockLength, blockLength);
    yGm.SetGlobalBuffer(y + block_idx * blockLength, blockLength);
    zGm.SetGlobalBuffer(z + block_idx * blockLength, blockLength);

    AscendC::LocalMemAllocator<AscendC::Hardware::UB> ubAllocator;        // ② 自主分配 UB（本讲 4.2）
    AscendC::LocalTensor<float> xLocal = ubAllocator.Alloc<float, blockLength>();
    AscendC::LocalTensor<float> yLocal = ubAllocator.Alloc<float, blockLength>();
    AscendC::LocalTensor<float> zLocal = ubAllocator.Alloc<float, blockLength>();

    AscendC::DataCopy(xLocal, xGm, blockLength);             // ③ 搬入（MTE2，本讲 4.1）
    AscendC::DataCopy(yLocal, yGm, blockLength);
    AscendC::PipeBarrier<PIPE_ALL>();                        // ④ 手动屏障：MTE2 → V（本讲 4.3）

    AscendC::Add(zLocal, xLocal, yLocal, blockLength);       // ⑤ 矢量计算（V 管线，u4-l1 详讲）
    AscendC::PipeBarrier<PIPE_ALL>();                        // ⑥ 手动屏障：V → MTE3

    AscendC::DataCopy(zGm, zLocal, blockLength);             // ⑦ 搬回（MTE3）
    AscendC::PipeBarrier<PIPE_ALL>();                        // ⑧ 手动屏障：MTE3 → 核结束
}
```

标注 ②③④⑥⑦⑧ 全是本讲的内容。删掉 ④（第一处屏障）会怎样？`Add` 跑在 V 管线，可能赶在 MTE2 把 `xLocal`/`yLocal` 写完之前就读它们——读到未初始化的 UB，结果错乱。删掉 ⑥ 则 `DataCopy(zGm, zLocal, ...)` 可能在 `Add` 算完前就把 `zLocal` 搬走，搬的是旧数据。这就是「自主范式必须自己管同步」的代价。

#### 4.3.4 代码实践

**实践目标**：通过「删一个屏障」反向体会同步的必要性（思考型，不必真跑出错）。

**操作步骤**（源码阅读型）：

1. 读 [add.asc:42-47](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L42-L47)。
2. 假设删掉第 44 行的 `PipeBarrier<PIPE_ALL>()`，推理：MTE2（搬 `xLocal`/`yLocal`）与 V（`Add` 读它们）失去顺序保证。
3. 再假设删掉第 47 行的屏障，推理：V（写 `zLocal`）与 MTE3（搬 `zLocal` 回 GM）失去顺序保证。

**需要观察的现象**：失去屏障后，正确性不再有保证——可能对、可能错，取决于硬件当时的调度时序（这正是「数据竞争」的典型表现：不是必错，而是偶发错）。

**预期结果**：你能说清「每处 `PipeBarrier` 保护的是哪两条流水线之间的边界」，并能指出 add 里三处屏障分别对应 `MTE2→V`、`V→MTE3`、`MTE3→结束`。

> 待本地验证：在 CPU 调试模式下，CPU 仿真通常是顺序执行的，删屏障可能仍跑对（因为仿真不模拟乱序）。要真正复现竞争需在 NPU 上跑——所以「CPU 调试通过」不等于「NPU 上正确」，这也是自主范式易踩坑的根源。

#### 4.3.5 小练习与答案

**练习 1**：自主式和框架式内存管理，各自把「分配」和「同步」交给谁？

**答案**：

| 维度 | 自主式（`LocalMemAllocator`） | 框架式（`TPipe/TQue`） |
|---|---|---|
| 分配 | 开发者手动 `Alloc`，自己定大小/块数 | `AllocTensor`，框架按 `InitBuffer` 配置分配 |
| 同步 | 开发者手动 `PipeBarrier` | `EnQue`/`DeQue` 队列语义自动插同步 |
| 灵活度 | 高（任意缓冲布局） | 受队列模型约束 |
| 出错风险 | 高（漏屏障→竞争） | 低（框架兜底） |

**练习 2**：`PipeBarrier<PIPE_ALL>()` 和 `SetFlag<...>/WaitFlag<...>` 有什么取舍？

**答案**：`PipeBarrier<PIPE_ALL>()` 等待**所有**流水线，最安全但最保守——它会让无关的流水线也空等，损失并行度。`SetFlag/WaitFlag`（[kernel_operator_block_sync_intf.h:42-46](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_block_sync_intf.h#L42-L46)）只同步**指定的两条**流水线（如「等 MTE2 完成再让 V 开始」），更精细、并行损失小，但要开发者自己配对事件。add 用 `PIPE_ALL` 是为简洁；性能敏感的算子会改用精细同步（u7-l1 详讲）。

---

## 5. 综合实践

把 4.1～4.3 串起来，独立写一个完整的小算子。

**任务**：参照 [add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc)，写一个「**逐元素放大 2 倍**」算子：输入 `x`，输出 `z = 2 * x`。要求用 `LocalMemAllocator` 申请 UB 缓冲，把 GM 数据搬到 UB、原地放大 2 倍、再搬回 GM，并在每个跨流水线边界插入 `PipeBarrier`。

**操作步骤**：

1. 复制一份 `add.asc`，把 Kernel 改成下面的样子（**示例代码**，替换原 `add_custom`，单输入单输出）：

   ```cpp
   // 示例代码：z = 2 * x，演示「自主分配 UB + DataCopy 搬运 + 手动 PipeBarrier」
   template <uint32_t blockLength>
   __vector__ __global__ void scale2_custom(__gm__ float* x, __gm__ float* z)
   {
       AscendC::InitSocState();

       AscendC::GlobalTensor<float> xGm, zGm;                 // 描述 GM
       xGm.SetGlobalBuffer(x + block_idx * blockLength, blockLength);
       zGm.SetGlobalBuffer(z + block_idx * blockLength, blockLength);

       AscendC::LocalMemAllocator<AscendC::Hardware::UB> ubAllocator;     // 自主分配一块 UB
       AscendC::LocalTensor<float> xLocal = ubAllocator.Alloc<float, blockLength>();

       AscendC::DataCopy(xLocal, xGm, blockLength);           // ① GM→UB（MTE2）
       AscendC::PipeBarrier<PIPE_ALL>();                      // ② 等 MTE2 写完

       AscendC::Muls(xLocal, xLocal, (float)2.0, (int32_t)blockLength); // ③ 原地 ×2（V 管线）
       AscendC::PipeBarrier<PIPE_ALL>();                      // ④ 等 V 算完

       AscendC::DataCopy(zGm, xLocal, blockLength);           // ⑤ UB→GM（MTE3）
       AscendC::PipeBarrier<PIPE_ALL>();                      // ⑥ 等 MTE3 写完
   }
   ```

   说明：

   - `Muls(dst, src, scalar, count)` 是「`dst[i] = src[i] * scalar`」的矢量接口（[kernel_operator_vec_binary_scalar_intf.h:201-203](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_binary_scalar_intf.h#L201-L203)），这里 `dst` 与 `src` 都是 `xLocal`，即原地放大。注意它的 `count` 是 `int32_t`，故对 `blockLength` 做了转换。
   - 也可以不用 `Muls`，改用 `AscendC::Add(xLocal, xLocal, xLocal, blockLength)`（\(x + x = 2x\)），效果等价、且和 add 样例用的接口完全一致，更稳妥。

2. 同步改 Host 侧 `kernel_add` 与 `main`：把两输入（`x`、`y`）减成一个输入（`x`），`<<<numBlocks, 0, stream>>>` 调用改成 `scale2_custom<blockLength><<<numBlocks, 0, stream>>>(xDevice, zDevice)`。
3. 改 golden 计算：`golden[i] = x[i] * 2.0f`（即 \(2x\)）。
4. 保持 `totalLength == numBlocks × blockLength`（u2-l3 的约束），按 u2-l3 流程 `source set_env.sh → cmake → make → ./demo`。

**需要观察的现象**：

- **结果正确性**：输出应满足 `output[i] == 2 * x[i]`，程序打印 `test pass!`。
- **UB 占用**：本算子只需 1 块 UB 缓冲（8 KB，当 `blockLength=2048`），比 add 的 3 块（24 KB）更省——因为单输入 + 原地计算复用了同一块缓冲。
- **同步结构**：Kernel 里恰好三处 `PipeBarrier`，分别保护 `MTE2→V`、`V→MTE3`、`MTE3→结束`，与 4.3.2 的标准骨架一一对应。

**预期结果**：你能独立写出「描述 GM → 自主分配 UB → DataCopy 搬入 → 同步 → 计算 → 同步 → DataCopy 搬回 → 同步」的完整链路，并说清每一处屏障保护的是哪两条流水线。

> 待本地验证：若手头无 NPU，至少在 CPU 调试模式（`CMAKE_ASC_RUN_MODE=cpu`）下编译运行通过，确认逻辑正确。注意 4.3.4 提到的「CPU 仿真不模拟乱序」，所以 CPU 通过只验证了数据流正确性，不能验证同步是否充分。

**进阶思考**（为下一讲铺垫）：这个算子全程只用一块 UB、串行搬一轮。如果输入很大（一轮搬不下），要怎么办？——答案就是 **Tiling 分块**：把 `blockLength` 切小、外面套循环、每轮搬一块算一块。这正是 u6-l1/u6-l2 的主题。而如果你嫌手写 `PipeBarrier` 繁琐，想让框架自动管同步，那就是 u5-l1 的 `TPipe/TQue`。

## 6. 本讲小结

- `DataCopy` 是批量搬运接口，规则是「**先 dst 后 src**」，搬运方向由参数类型隐式决定：`DataCopy(Local, Global, ...)` 是 GM→UB（走 **MTE2**），`DataCopy(Global, Local, ...)` 是 UB→GM（走 **MTE3**），流水线映射直接写在声明的 `__inout_pipe__` 标注里。
- `DataCopy` 按粒度分三级：Level 2（`count`，最简单，初学首选）、Level 1（`SliceInfo` 多维）、Level 0（`DataCopyParams` 控 repeat/gap，极致性能）；数据需 32 字节对齐，零头用 `DataCopyPad`。
- `LocalMemAllocator<Hardware::UB>` 是**栈式 bump allocator**：内部仅一个游标 `head_`，每次 `Alloc` 在 `head_` 处切出 `位宽×元素数/8` 字节再把游标前推；线性分配、Kernel 内不回收；构造时校验 `hard != GM/MAX`，分配时校验逻辑位置与硬件位置一致。
- add 用的 `Alloc<T, tileSize>()` 由 `GetDefaultPosition(hard)` 推断位置（UB→`VECCALC`）；位置-硬件一致性靠 `GetPhyType` 桥接（与 u3-l1/u3-l2 呼应）。
- **自主内存管理范式 = 自己管分配 + 自己管同步**：MTE2/MTE3 与 V 是独立流水线，跨流水线边界必须手动 `PipeBarrier`；这与框架式（`TPipe/TQue` 由 `EnQue`/`DeQue` 自动管同步）形成对照。
- `PipeBarrier<PIPE_ALL>()` 是保守的全屏障（等所有流水线），安全但损失并行；精细同步用 `SetFlag`/`WaitFlag`（u7-l1）。

## 7. 下一步学习建议

- **学 u4-l1（矢量计算接口）**：本讲把 `Add`/`Muls` 当「V 管线上的计算」一笔带过，下一讲会打开矢量计算接口体系，讲清双目/单目/比较选择接口、元素计数 `count` 与对齐约束，那时你就能自如地组合搬运 + 计算。
- **学 u5-l1（TPipe/TQue 框架）**：本讲的 `LocalMemAllocator + PipeBarrier` 是自主范式；学完 u5-l1 后，强烈建议把本讲综合实践的「放大 2 倍」算子**用 `TQue` 重写一遍**，亲身对比「手写屏障」与「队列自动同步」的代码量与心智负担。
- **学 u6-l1/u6-l2（多核并行与 Tiling）**：本讲的综合实践只搬一轮、用一块 UB；当输入超过 UB 容量时就要 Tiling 分块循环搬运，并把数据切到多核上并行——那是把本讲的单核单块骨架扩展到真实大算子的关键一步。
- **延伸阅读**：想看更复杂的搬运形态，可先扫一眼 [kernel_operator_data_copy_intf.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_data_copy_intf.h) 里 Level 0/1 的重载和 `DataCopyPad`/`Nd2Nz`，建立「除了 count 模式还有更强大的搬运控制」的印象，具体用法留待进阶讲义。
