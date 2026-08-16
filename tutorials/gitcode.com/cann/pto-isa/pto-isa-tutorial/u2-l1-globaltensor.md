# GlobalTensor：全局内存上的张量视图

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `GlobalTensor` 是什么：它只是一个「GM 指针 + 形状/步长元数据」的轻量视图，本身不搬运任何数据。
2. 掌握 `pto::Shape<...>` / `pto::Stride<...>` 的 5 维模板设计，理解 `DYNAMIC`（`-1`）维度的「静态进类型、动态进运行时」机制。
3. 会用维度与步长（stride 以**元素**为单位）手工推算任意多维下标对应的内存偏移。
4. 能独立写一段代码，用 `GlobalTensor` 描述 GM 上的数据，并从不同视角（不同 Shape）观察同一块内存。

本讲是单元二「编程模型核心」的第一讲。在 u1-l4 中你已经见过 kernel 里 `GlobalTensor` 与 `TLOAD`/`TSTORE` 配合的样子；本讲我们把这个「视图」本身拆开看透。

## 2. 前置知识

本讲默认你已读过 u1-l4（Add 算子逐行精读），知道以下概念（不熟悉请先回看）：

- **GM（Global Memory）**：设备上的全局内存，即 kernel 入口拿到的 `__gm__ T*` 指针指向的那片空间。类比 CPU 世界的「堆内存」。
- **`__gm__`**：地址空间标注，告诉编译器这个指针指向 GM。CPU 仿真（`__CPU_SIM`）下它退化为普通指针。
- **视图（view）**：不拥有数据、只描述「数据长什么样、在哪里」的轻量对象。类比 PyTorch 里 `tensor.view(...)`：改的是描述方式，不是数据本身。
- **shape / stride**：shape 是每个维度多长；stride 是「该维下标 +1 时，内存地址跳过多少个**元素**」。例如行主序矩阵第 \(i\) 行第 \(j\) 列的元素地址偏移为 \(i \cdot \text{ld} + j\)，其中 `\ld`（leading dimension）就是行的 stride。
- **模板参数与编译期常量**：C++ 模板参数在编译期确定。PTO 利用这一点，把「能提前确定的维度」编码进类型，把「运行时才知道的维度」留成 `-1` 占位。

一个直觉类比：`GlobalTensor` 之于 GM 数据，就像「地图 + 图例」之于真实地形——TLOAD 这类搬运指令拿着这张地图，才知道该去 GM 的哪些位置、按什么间距取数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/pto/common/pto_tile.hpp](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp) | 核心头文件：定义 `DYNAMIC`、`Shape`、`Stride`、`GlobalTensor`、`TileShape2D`/`BaseShape2D` 以及 `Tile`（Tile 留到下一讲） |
| [include/pto/common/type.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L432-L439) | 定义 `GlobalTensorDim` 命名空间（`DIM_0`~`DIM_4`、`TOTAL_DIM=5`）与 `pto::half` 等标量类型 |
| [docs/coding/GlobalTensor.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/GlobalTensor.md) | 官方编程模型文档，本讲的概念框架与其对齐 |

阅读姿势：先看 4.1 建立「视图」直觉，再带着问题去读 4.2/4.3 的模板代码——`pto_tile.hpp` 有 1800+ 行，但本讲只涉及前 820 行左右。

## 4. 核心概念与源码讲解

### 4.1 GlobalTensor 视图

#### 4.1.1 概念说明

`pto::GlobalTensor` 建模「存放在 GM 中的一个张量」。它是对两样东西的轻量包装：

1. 一个 `__gm__` 指针；
2. 一份 **5 维**的 shape/stride 描述。

关键认知：**`GlobalTensor` 不搬数据**。真正读写 GM 的是 `TLOAD`、`TSTORE`、`MGATHER`、`MSCATTER` 等搬运指令；`GlobalTensor` 只是这些指令消费的「元数据」（见 [docs/coding/GlobalTensor.md:L3-L8](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/GlobalTensor.md#L3-L8)）。所以构造一个 `GlobalTensor` 是零成本操作，你可以在 kernel 里随意创建新视角来看同一块 GM 内存。

为什么要这样设计？因为 PTO 的编程模型是「GlobalTensor（GM 视图）→ Tile（片上缓冲）」两级：视图负责描述 GM 里数据的空间结构，Tile 负责承接搬上来的数据。两者解耦后，同一份 `TLOAD` 指令可以服务于任意形状的 GM 布局。

#### 4.1.2 核心流程

一个 `GlobalTensor` 的典型生命周期：

```text
① 类型定义：using GT = GlobalTensor<元素类型, Shape<...>, Stride<...>, Layout::ND>
② 构造：GT t(ptr, shape运行时值, stride运行时值)   ← 只填 DYNAMIC 维
③ 消费：TLOAD(tile, t) / TSTORE(t, tile) 等指令读取它的元数据寻址
④ （可选）改视角：用不同 Shape/Stride 的 GT 包同一个 ptr
```

多维下标到地址偏移的换算公式（stride 单位是元素）：

\[
\text{offset}(i_0,i_1,i_2,i_3,i_4) = \sum_{d=0}^{4} i_d \cdot \text{stride}[d]
\]

#### 4.1.3 源码精读

`GlobalTensor` 的四个模板参数（[include/pto/common/pto_tile.hpp:L272-L278](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L272-L278)）：

```cpp
template <typename Element_, typename Shape_, typename Stride_, Layout Layout_ = Layout::ND>
struct GlobalTensor {
    using Shape = Shape_;
    using Stride = Stride_;
    using RawDType = remove_gm_t<Element_>;
    using DType = __gm__ RawDType;
    static constexpr Layout layout = Layout_;
```

这段代码做了什么：`Element_` 是 GM 中元素的标量类型；`RawDType` 通过 `remove_gm` trait 剥掉可能传入的 `__gm__` 修饰，再统一加回 `__gm__` 得到指针类型 `DType`；`Layout_` 是布局**提示**（`ND`/`DN`/`NZ`/`MX_*` 等），供后端 lower 时走目标特定的快路径。

构造函数只填充 DYNAMIC 维（[include/pto/common/pto_tile.hpp:L291-L309](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L291-L309)）：

```cpp
PTO_INTERNAL GlobalTensor(DType* data, const Shape& shape = defaultShape, const Stride& stride = defaultStride)
{
    data_ = data;
    if constexpr (staticShape[GlobalTensorDim::DIM_0] == DYNAMIC) {
        shape_.shape[GlobalTensorDim::DIM_0] = shape.shape[GlobalTensorDim::DIM_0];
    }
    ...
```

这段代码做了什么：把指针存进 `data_`，然后逐维用 `if constexpr` 判断——只有静态形状为 `DYNAMIC` 的维度才从运行时参数拷贝值，静态维度直接由类型提供，构造时传了也会被忽略。

CPU 仿真专用的 `GetElement` 是理解「视图如何寻址」的最佳窗口（[include/pto/common/pto_tile.hpp:L584-L591](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L584-L591)）：

```cpp
DType GetElement(int64_t i0, int64_t i1, int64_t i2, int64_t i3, int64_t i4)
{
    const auto offset = i0 * GetStride(GlobalTensorDim::DIM_0) + i1 * GetStride(GlobalTensorDim::DIM_1) +
                        i2 * GetStride(GlobalTensorDim::DIM_2) + i3 * GetStride(GlobalTensorDim::DIM_3) +
                        i4 * GetStride(GlobalTensorDim::DIM_4);
    return GetProperDataPart(data_, offset);
}
```

这段代码做了什么：就是上面偏移公式的直接实现——5 个下标各乘各的 stride 求和，得到元素偏移。NPU 真机上没有这个函数（寻址由搬运指令的硬件描述符完成），但**语义完全一致**。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「`GlobalTensor` 只是视图，同一指针可以套不同视角」。

**操作步骤**（示例代码，仿照 [docs/coding/GlobalTensor.md:L92-L108](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/GlobalTensor.md#L92-L108) 的最小例子改写）：

1. 新建 `gt_view_test.cpp`（放在仓库任意临时目录，不要放进 `include/`）：

```cpp
// 示例代码：验证 GlobalTensor 的「一指针多视角」
#include <cstdio>
#include <vector>
#include <pto/pto-inst.hpp>
using namespace pto;

int main() {
    std::vector<pto::half> buf(128 * 256, pto::half(1.0)); // 模拟 GM 上的一块内存
    auto* ptr = buf.data();

    // 视角一：把 buf 看成 [128, 256] 矩阵（行/列为动态维度）
    using GTMat = GlobalTensor<pto::half, Shape<1,1,1,-1,-1>, Stride<1,1,1,-1,1>, Layout::ND>;
    GTMat mat(ptr, Shape(128, 256), Stride(256));          // 行 stride=256, 列 stride=1

    // 视角二：把同一块内存看成 [2, 64, 256]（完全静态，无需运行时参数）
    using GTB   = GlobalTensor<pto::half, Shape<1,1,2,64,256>, Stride<1,1,64*256,256,1>, Layout::ND>;
    GTB batch(ptr);

    std::printf("mat    shape: [%lld, %lld]\n",
                (long long)mat.GetShape(GlobalTensorDim::DIM_3),
                (long long)mat.GetShape(GlobalTensorDim::DIM_4));
    std::printf("batch  shape: [%d, %d, %d] (constexpr)\n",
                GTB::GetShape<GlobalTensorDim::DIM_2>(),
                GTB::GetShape<GlobalTensorDim::DIM_3>(),
                GTB::GetShape<GlobalTensorDim::DIM_4>());
    return 0;
}
```

2. 用 CPU 仿真的宏与头文件路径编译运行（具体编译选项需与本机 CANN/CPU 仿真环境匹配，**待本地验证**）：

```bash
g++ -std=c++20 -D__CPU_SIM -I include gt_view_test.cpp -o gt_view_test && ./gt_view_test
```

**需要观察的现象**：两个视角共享同一个 `ptr`，但打印出的 shape 不同；视角二的 shape 是 `constexpr` 编译期常量，不经过任何运行时数组。

**预期结果**：

```text
mat    shape: [128, 256]
batch  shape: [2, 64, 256] (constexpr)
```

若编译报找不到头文件，请检查 `-I` 是否指向仓库根目录（使 `pto/pto-inst.hpp` 可解析）；`pto::half` 在 CPU 侧是 `_Float16` 的别名（[include/pto/common/type.hpp:L448](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L448)）。

#### 4.1.5 小练习与答案

**练习 1**：`GlobalTensor` 构造函数会拷贝 GM 数据吗？构造一个 `GlobalTensor` 的开销大概是什么量级？

**答案**：不会拷贝任何数据。构造只做「存指针 + 按 `if constexpr` 填充 DYNAMIC 维的几个 `int64_t`」，是 O(1) 的零拷贝操作；真正搬数据的是 TLOAD/TSTORE 等指令。

**练习 2**：为什么 `GlobalTensor` 的 `Layout` 只是「提示（hint）」，而 Tile 的布局是强约束？

**答案**：`GlobalTensor` 描述的是 GM 上已有的存储形态，`Layout` 用来让后端在 lower 时选择目标特定快路径（如 `NZ` 对 Cube 友好的排布）；Tile 布局则决定片上缓冲的真实数据组织，直接影响指令的正确性，所以是强约束。另外 GlobalTensor 是 5 维的，Tile 的「外层 + 内层盒式」二维布局无法覆盖所有 GM 场景（[docs/coding/GlobalTensor.md:L65-L74](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/GlobalTensor.md#L65-L74)）。

### 4.2 Shape 模板

#### 4.2.1 概念说明

PTO 把 GM 张量统一表示为 **5 维**对象。`pto::Shape<N1,N2,N3,N4,N5>` 存 5 个维度长度，每个模板参数要么是编译期常量，要么是 `pto::DYNAMIC`（即 `-1`）：

- **静态维度**：编码在类型里，通过 `Shape::staticShape[dim]`（以及派生到 `GlobalTensor::staticShape`）在编译期读取，零运行时成本，且编译器可据此优化。
- **动态维度**：存进运行时数组 `Shape::shape[dim]`，由构造函数在运行时填充。

这套「混合静态/动态」设计的动机：tile 形状、对齐要求等 kernel 内已知的信息走静态（性能 + 编译期检查）；矩阵的 M/N 等只有启动时才知道的信息走动态（灵活性）。`DYNAMIC` 的定义只有一行（[include/pto/common/pto_tile.hpp:L28](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L28)）：

```cpp
constexpr int DYNAMIC = -1;
```

维度编号 `DIM_0`~`DIM_4` 定义在 [include/pto/common/type.hpp:L432-L439](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L432-L439)。常见的 2-D 用法把前三维设为 1，用最后两维表示 `(rows, cols)`。

#### 4.2.2 核心流程

`Shape` 的构造规则（以 2 参数构造为例）：

```text
Shape<1,1,1,-1,-1> s(128, 256);
  ① static_assert：动态维个数（2）必须等于实参个数（2），否则编译失败
  ② 从左到右，把实参依次填给每个 DYNAMIC 维（DIM_3=128, DIM_4=256）
  ③ 静态维（1,1,1）不占实参、不可赋值
```

这个「参数个数 = 动态维个数」的检查是 `static_assert`，意味着传错参数个数会在**编译期**被拦下，而不是运行时出错。

#### 4.2.3 源码精读

`Shape` 模板主体（[include/pto/common/pto_tile.hpp:L30-L45](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L30-L45)）：

```cpp
template <int64_t N1 = DYNAMIC, int64_t N2 = DYNAMIC, int64_t N3 = DYNAMIC,
          int64_t N4 = DYNAMIC, int64_t N5 = DYNAMIC>
struct Shape {
    static constexpr int64_t staticShape[5] = {N1, N2, N3, N4, N5};
    PTO_INTERNAL Shape(int64_t n1, int64_t n2, int64_t n3, int64_t n4, int64_t n5)
    {
        if constexpr (N1 == DYNAMIC)
            shape[GlobalTensorDim::DIM_0] = n1;
        ...
```

这段代码做了什么：`staticShape` 把 5 个模板参数原样存成编译期数组；5 参数构造函数对每一维做 `if constexpr (Nx == DYNAMIC)` 判断，只给动态维赋运行时值。

参数个数强校验（[include/pto/common/pto_tile.hpp:L79-L98](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L79-L98)）：

```cpp
PTO_INTERNAL Shape(int64_t n1, int64_t n2)
{
    static_assert(
        (N1 == DYNAMIC) + (N2 == DYNAMIC) + (N3 == DYNAMIC) + (N4 == DYNAMIC) + (N5 == DYNAMIC) ==
            GlobalTensorDim::DIM_2,
        "2-parameter constructors is only applicable to Stride with 2 dynamic dimension.");
    int idx = 0;
    const int64_t vals[] = {n1, n2};
    if constexpr (N1 == DYNAMIC)
        shape[GlobalTensorDim::DIM_0] = vals[idx++];
    ...
```

这段代码做了什么：用「布尔求和」数出动态维个数，`static_assert` 它必须恰好等于 2；然后用 `idx` 游标把两个实参从左到右分配给各个动态维。1/3/4 参数版本的构造函数结构完全相同。

`GlobalTensor` 上的两种读取方式对比——运行时接口（[include/pto/common/pto_tile.hpp:L329-L345](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L329-L345)）：

```cpp
PTO_INTERNAL int64_t GetShape(const int dim)
{
    switch (dim) {
        case GlobalTensorDim::DIM_0:
            return GetShapeSize<staticShape[GlobalTensorDim::DIM_0]>(dim);
        ...
```

编译期接口（[include/pto/common/pto_tile.hpp:L365-L374](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L365-L374)）：

```cpp
template <int dim>
AICORE static constexpr int64_t GetShape()
{
    static_assert(dim >= GlobalTensorDim::DIM_0 && dim < GlobalTensorDim::TOTAL_DIM, "only support get dim(0-4)");
    if constexpr (dim == GlobalTensorDim::DIM_0) {
        static_assert(staticShape[GlobalTensorDim::DIM_0] != DYNAMIC,
                      "dim 0 is dynamic, cannot be obtained using the template interface.");
        return staticShape[GlobalTensorDim::DIM_0];
    }
```

这段代码做了什么：`GetShape(dim)` 运行时函数经 `GetShapeSize` 私有助手折中处理——静态维直接返回常量、动态维查运行时数组；模板版 `GetShape<dim>()` 是 `constexpr`，但对动态维会 `static_assert` 报错，强制你只能对静态维做编译期查询。

此外 `GlobalTensor` 还提供 `SetShape<dim>(...)` / `SetStride<dim>(...)` 系列（如 [include/pto/common/pto_tile.hpp:L439-L445](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L439-L445)），带 `static_assert(staticShape[dim] == DYNAMIC, "dim must be DYNAMIC")` 保护——静态维不允许运行时改写。u1-l4 里 Add kernel 循环中「更新视图地址/形状」用的正是这类接口。

#### 4.2.4 代码实践

**实践目标**：体验 `static_assert` 的参数个数校验。

**操作步骤**（示例代码）：

1. 写 3 行测试：

```cpp
using namespace pto;
Shape<1,1,1,-1,-1> a(128, 256);   // ✅ 2 个动态维，2 个实参
Shape<1,1,1,-1,-1> b(128);        // ❌ 编译失败：1-parameter constructor 要求恰好 1 个动态维
Shape<1,1,1,128,256> c;           // ✅ 全静态，默认构造即可
```

2. 分别注释/放开各行编译，观察编译器输出。

**需要观察的现象**：第 2 行触发 `static_assert`，错误信息正是源码里的英文字符串 `"1-parameter constructors is only applicable to Stride with 1 dynamic dimension."`。

**预期结果**：错误在编译期出现，且能精确定位到模板实例化处。**待本地验证**（不同编译器报错格式略有差异）。

#### 4.2.5 小练习与答案

**练习 1**：`Shape<1,1,1,-1,-1>` 和 `Shape<1,1,1,128,256>` 是同一个类型吗？`sizeof` 各是多少？

**答案**：不是同一类型，模板参数不同即不同类型。前者含运行时数组 `int64_t shape[5]`（约 40 字节），后者虽然也继承了同样的成员，但维度值全在 `staticShape` 编译期数组里，运行时数组永远不会被有意义地使用。二者不兼容赋值，需要在构造时显式传值。

**练习 2**：为什么 2-D 用法把 shape 写成 `Shape<1,1,1,rows,cols>` 而不是 `Shape<rows,cols>`？

**答案**：PTO 的 GlobalTensor 统一是 5 维模型，DIM_0~DIM_2 恒置 1，把行放在 DIM_3、列放在 DIM_4（最内维）。这样 TLOAD 等指令的寻址逻辑只需实现一套 5 维通式，2-D 只是特例；同时也给 3-D/4-D 场景（如卷积 NCHW、批量矩阵）预留了维度空间。

### 4.3 维度与步长

#### 4.3.1 概念说明

`pto::Stride<SN1,...,SN5>` 与 `Shape` 结构完全同构，存的是每维的**步长**：

- 单位是**元素**，不是字节。想换算字节需自己乘 `sizeof(Element)`。
- 步长描述「该维下标 +1 时跳过多少元素」，因此最内维（通常是 DIM_4）的步长一般为 1。
- 与 Shape 一样支持 DYNAMIC 混合静态/动态。

**ld（leading dimension）** 是最常见的动态步长：GM 上的矩阵常按固定行宽分配（如 4096 对齐），实际逻辑列数可能小于行宽，行 stride 必须取分配宽度而不是逻辑列数——这就是为什么 `Stride<1,1,1,-1,1>` 把 DIM_3 留成动态：每份输入的 ld 都可能不同。

`Stride` 的定义（[include/pto/common/pto_tile.hpp:L144-L160](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L144-L160)）：

```cpp
template <int64_t SN1 = DYNAMIC, ..., int64_t SN5 = DYNAMIC>
struct Stride {
    static constexpr int64_t staticStride[GlobalTensorDim::TOTAL_DIM] = {SN1, SN2, SN3, SN4, SN5};
    PTO_INTERNAL Stride(int64_t n1, int64_t n2, int64_t n3, int64_t n4, int64_t n5)
    {
        if constexpr (SN1 == DYNAMIC)
            stride[GlobalTensorDim::DIM_0] = n1;
        ...
```

#### 4.3.2 核心流程

以一块 `[128, 256]` 的 fp16 行主序矩阵为例，各维取值：

| 维度 | 含义 | shape | stride（元素） | stride（字节，fp16=2B） |
| --- | --- | --- | --- | --- |
| DIM_0~DIM_2 | 占位 | 1 | 任意（下标恒 0） | — |
| DIM_3 | 行 | 128 | 256（= ld） | 512 |
| DIM_4 | 列 | 256 | 1 | 2 |

按公式验证：元素 `(i, j)` 的偏移 = \(i \times 256 + j \times 1\)，与 C 二维数组 `a[i][j]` 的寻址一致。

若把同一块内存看成 `[2, 64, 256]`（2 个 batch，每 batch 64 行），则 DIM_2 的 stride 应为 \(64 \times 256 = 16384\)：batch 之间正好隔一整个 64 行块。

仓库还提供了 2-D 快捷助手，帮你免手写这些数字（[docs/coding/GlobalTensor.md:L76-L84](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/GlobalTensor.md#L76-L84)）：

- `pto::TileShape2D<T, rows, cols, layout>`：生成一个 `Shape<1,1,1,rows,cols>`（`NZ`/`MX_*` 布局则生成对应的分块形状）。
- `pto::BaseShape2D<T, rows, cols, layout>`：**名字带 Shape，实际派生自 `Stride`**，生成配套的步长。

#### 4.3.3 源码精读

ND 布局的 `TileShape2D`（[include/pto/common/pto_tile.hpp:L718-L727](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L718-L727)）：

```cpp
template <typename T, int64_t rows, int64_t cols>
struct TileShape2D<T, rows, cols, Layout::ND>
    : public Shape<1, 1, 1, GetShape2DRows<T, rows>(), GetShape2DCols<T, cols>()> {
    ...
    PTO_INTERNAL TileShape2D(int64_t dynamicRows, int64_t dynamicCols) : Parent(1, 1, 1, dynamicRows, dynamicCols) {}
```

这段代码做了什么：把用户视角的 `(rows, cols)` 映射到 5 维的 `(DIM_3, DIM_4)`，`DYNAMIC` 会被透传（`GetShape2DRows` 对 `DYNAMIC` 原样返回 `-1`），动态场景用双参构造函数填值。

ND 布局的 `BaseShape2D`（[include/pto/common/pto_tile.hpp:L789-L804](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L789-L804)）：

```cpp
template <typename T, int64_t rows, int64_t cols>
struct BaseShape2D<T, rows, cols, Layout::ND>
    : public Stride<
          GetBaseShape2DStride0<T, rows, cols>(), GetBaseShape2DStride0<T, rows, cols>(),
          GetBaseShape2DStride0<T, rows, cols>(), GetShape2DCols<T, cols>(), 1> {
    ...
    PTO_INTERNAL BaseShape2D(int64_t dynamicRows, int64_t dynamicCols)
        : Parent(dynamicRows * dynamicCols, dynamicRows * dynamicCols, dynamicRows * dynamicCols, dynamicCols, 1) {}
```

这段代码做了什么：为连续（行主序）存储生成步长——DIM_0~DIM_2 都取总元素数 `rows*cols`（这几维下标恒为 0，取多大都不影响寻址），DIM_3（行）取 `cols`，DIM_4（列）取 1。对照偏移公式即可验证这与手写的 `Stride<1,1,1,256,1>` 语义等价（`1*anything = 0 贡献`）。

再看一个非平凡布局：NZ（Cube 友好的分块排布）的 `TileShape2D` 会把形状重排成 16×C0 的分块五元组（[include/pto/common/pto_tile.hpp:L682-L698](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L682-L698)）：

```cpp
template <typename T, int64_t rows, int64_t cols>
struct TileShape2D<T, rows, cols, Layout::NZ>
    : public Shape<1, GetTileShape2DNZCols<T, cols>(), GetTileShape2DNZRows<T, rows>(),
                   FRACTAL_NZ_ROW, C0_SIZE_BYTE / sizeof(T)> {
    static_assert((rows == DYNAMIC) || (rows % FRACTAL_NZ_ROW == 0), "rows must be divisible by 16 for Layout::NZ");
```

这段代码做了什么：把 `[rows, cols]` 逻辑矩阵改写为「`rows/16 × cols/C0` 个 16 行 × C0 列小方块」的 5 维描述，并用 `static_assert` 强制 rows 是 16 的倍数——这就是「布局提示 + 形状重写」配合的实例，NZ 布局的细节在后续 Cube 指令讲义（u5-l1）会再展开。

#### 4.3.4 代码实践

**实践目标**：手工推导 stride，并用 `BaseShape2D` 交叉验证。

**操作步骤**（示例代码）：

1. 对 `[128, 256]` 的 fp16 矩阵，先手算：行 stride = 256 元素 = 512 字节；总大小 = 128×256×2 = 65536 字节。
2. 再用助手生成并打印对比：

```cpp
using namespace pto;
using ShapeT = TileShape2D<pto::half, 128, 256, Layout::ND>;  // → Shape<1,1,1,128,256>
using StrideT = BaseShape2D<pto::half, 128, 256, Layout::ND>; // → Stride<32768,32768,32768,256,1>
static_assert(ShapeT{}.GetShape == 0 || true); // 仅示意；实际用 GlobalTensor 包起来查询
std::printf("row stride = %lld elements\n", (long long)StrideT::staticStride[GlobalTensorDim::DIM_3]);
```

3. 编译运行（同样需 `__CPU_SIM` + `-I include`，**待本地验证**）。

**需要观察的现象**：打印的行 stride 应为 256，与你手算一致；DIM_0~DIM_2 的 stride 为 32768（= 128×256 总元素数），虽然不为 1，但因为这几维下标恒为 0，不影响寻址。

**预期结果**：手算与 `BaseShape2D` 生成值一致，验证你对「stride 单位是元素」「占位维 stride 无关紧要」两点理解正确。

#### 4.3.5 小练习与答案

**练习 1**：一个 `[64, 100]` 的 fp32 矩阵存放在 ld=128 的 GM 缓冲中（每行实际分配 128 个元素），DIM_3/DIM_4 的 stride 分别是多少？元素 `(50, 99)` 的字节偏移是多少？

**答案**：DIM_3 stride = 128（元素，取分配宽度 ld 而非逻辑列数 100），DIM_4 stride = 1。偏移 = (50×128 + 99×1) × 4 字节 = (6400 + 99) × 4 = 25996 字节。这个例子说明为什么 ld 类 stride 必须留 DYNAMIC。

**练习 2**：`BaseShape2D` 名字里是 Shape，为什么文档强调它是「stride 助手」？

**答案**：因为它派生自 `pto::Stride`（见上面源码 `: public Stride<...>`），生成的是步长描述而非形状；命名沿用了「描述一块 base 内存布局」的含义。读代码时看继承关系比看名字可靠。

## 5. 综合实践

把三个模块串起来的任务：**为同一块 GM 内存构建三个视角并验证寻址一致**。

任务描述：分配一块 128×256 的 fp16 缓冲，将元素 `(i, j)` 初始化为 `i * 1000 + j`。然后：

1. 视角 A：`GlobalTensor<half, Shape<1,1,1,-1,-1>, Stride<1,1,1,-1,1>>`，动态构造为 `[128, 256]`；
2. 视角 B：`GlobalTensor<half, Shape<1,1,2,64,256>, Stride<1,1,64*256,256,1>>`，全静态构造；
3. 用 CPU 仿真的 `GetElement`（[include/pto/common/pto_tile.hpp:L584-L591](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L584-L591)）分别在两个视角下读取「同一逻辑位置」，例如 A 的 `(70, 5)` 与 B 的 `(0, 6, 5)`，验证读到的值相同（因为 \(70 \times 256 + 5 = 0 \times 16384 + 6 \times 256 + 5\)）；
4. 再构造一个视角 C：把缓冲看成 `[2, 64, 256]` 但**行 stride 故意写错**为 255，观察读出的值错位，体会 stride 的意义。

检查点：视角 A/B 读值一致；视角 C 出现错位。若第 3 步数值对不上，回到 4.3.2 的偏移公式逐项排查。运行环境依赖本地 CPU 仿真编译，完整命令**待本地验证**。

## 6. 本讲小结

- `GlobalTensor` = `__gm__` 指针 + 5 维 shape/stride 元数据，是**零拷贝视图**；真正搬数据的 TLOAD/TSTORE 等指令消费这份元数据寻址。
- `pto::Shape<...>`/`pto::Stride<...>` 采用「静态维度进类型、动态维度（`DYNAMIC = -1`）进运行时数组」的混合设计，构造函数用 `static_assert` 强制「实参个数 = 动态维个数」。
- 多维下标到偏移的换算：\(\text{offset} = \sum_d i_d \cdot \text{stride}[d]\)，stride 单位是**元素**；最内维 stride 通常为 1，ld 类行宽必须留动态。
- 查询接口双轨制：运行时 `GetShape(dim)` 静态/动态通吃；编译期 `GetShape<dim>()` 是 `constexpr`，但对动态维会 `static_assert` 拒绝。
- 2-D 快捷助手 `TileShape2D`（生成 Shape）与 `BaseShape2D`（名字带 Shape、实际生成 Stride）覆盖 ND/DN/NZ/MX 等布局，NZ 等布局还会重写形状并附带整除约束。

## 7. 下一步学习建议

下一讲（u2-l2）将走向视图的对岸——**Tile 编程模型**：片上 2-D 缓冲抽象、静态 tile 形状与动态 tile 掩码（`RowMaskInternal`/`ColMaskInternal`）、以及 `TileType::Vec` 等 tile 类型。你会发现本讲的 `TileShape2D<..., Layout::NZ>` 正是连接 GlobalTensor 布局与 Tile 盒式布局的桥梁。

继续阅读建议：

- 源码：通读 [include/pto/common/pto_tile.hpp:L1389-L1703](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1389-L1703)（`Tile` 结构），留意其中 `ValidRow`/`ValidCol` 也可以是 `DYNAMIC`。
- 文档：[docs/coding/GlobalTensor.md](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/GlobalTensor.md) 的「Address binding (TASSIGN)」小节，预告 TASSIGN 如何把 GM 指针绑定进视图（u3-l2 详讲）。
- 回顾 u1-l4 的 Add kernel，用本讲的眼光重新看其中 `GlobalTensor` 的构造与循环内 `SetShape`/地址更新，确认你能解释每一行。
