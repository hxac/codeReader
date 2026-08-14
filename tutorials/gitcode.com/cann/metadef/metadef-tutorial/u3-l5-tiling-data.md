# u3-l5 TilingData：tiling 参数的序列化与传递

## 1. 本讲目标

在 u3-l3 中我们知道了：算子的 TilingFunc 通过 `TilingContext` 计算切分参数，并把结果写进一个叫 tiling data 的输出槽位。本讲专门回答「这个槽位里装的东西到底是什么、怎么装进去、装进去之后怎么被读回来」。学完本讲你应该能够：

- 说清 `gert::TilingData` 容器的内存布局（头部 + 连续数据区）与 `Append`/`Expand` 的追加式写入机制。
- 理解 `TilingContext::GetTilingData<T>()` 与 `GetRawTilingData()` 的区别，以及「覆写式」与「追加式」两种写入风格的适用场景。
- 掌握 `optiling` 命名空间下 `tilingdata_base.h` 的宏体系：`BEGIN_TILING_DATA_DEF` 如何在编译期生成一个带字段信息、可自对齐、可跨进程重建的 tiling 结构体。
- 能独立编写一个包含两个 int32 字段的最小 TilingData 序列化/反序列化用例并通过编译运行。

## 2. 前置知识

**什么是 tiling（切分）**：昇腾芯片上的计算核（AI Core）一次只能处理固定大小的数据块。算子执行前，框架要先把大张量切成小块，并算出每个核分多少数据、循环多少次。这些「切分参数」统称 tiling 参数。

**为什么需要序列化**：TilingFunc 运行在 host 侧（CPU），而算子 kernel 运行在 device 侧（NPU）。两边是不同的地址空间，只能通过一块**连续的字节流**传递参数。所以 tiling 结果必须被「拍平」成一串字节，device 侧再按相同的结构定义把它解释回来。这本质上和网络的 protobuf、持久化的 struct dump 是同一类问题——只不过这里的约束更苛刻：不能有虚表、不能有指针、不能依赖 STL 布局。

**POD / standard_layout**：C++ 术语，指内存布局可预测、可以用 `memcpy` 直接复制的类型。metadef 的执行期结构体大量使用 `static_assert(std::is_standard_layout<T>::value)` 把这一约束固化为编译期检查（u3-l1、u5-l4 已详细讲过）。TilingData 同样遵守这条纪律。

**追加式（append-only）写入**：想象一根往一个方向生长的字节管道——每次 `Append` 都把新数据接在尾部，同时把「已用长度」加一。读回时按写入顺序依次解释即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [inc/external/exe_graph/runtime/tiling_data.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h) | `gert::TilingData` 容器本体：容量管理、Append/Expand、以及 `AttrDataType` 类型转换枚举。全 header-only（模板部分）。 |
| [base/runtime/tiling_data.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc) | `TilingData::AppendConvertedAttrVal` 的实现：把算子属性按 (源类型 → 目标类型) 二维查表转换后追加进容器。 |
| [inc/external/register/tilingdata_base.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h) | `optiling` 命名空间的宏体系：`BEGIN_TILING_DATA_DEF` 等宏 + `TilingDef` 基类 + 工厂 `CTilingDataClassFactory`。 |
| [base/asc/tilingdata_base_impl.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc) | 上述宏体系基类方法的实现：对齐占位、数据区初始化、`SaveToBuffer` 序列化、工厂注册与查找。 |
| [tests/ut/base/testcase/tiling_data_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc) | 两者配套的单元测试，本讲代码实践的直接参照。 |

注意区分**两个同名的体系**，这是初学者最容易混淆的地方：

| | `gert::TilingData` | `optiling::TilingDef`（宏生成类） |
| --- | --- | --- |
| 头文件 | `exe_graph/runtime/tiling_data.h` | `register/tilingdata_base.h` |
| 定位 | 运行时**字节流容器**（只有 capacity/size/data 指针，不认识任何字段） | 宿主侧**结构化 tiling 参数**（每个字段有名字、类型、偏移） |
| 写入方式 | `Append` 逐段追加 | `set_xxx()` 按字段赋值，`SaveToBuffer` 一次性拍平 |
| 使用者 | exe_graph 执行图体系（gert 新体系） | 老的 optiling 注册体系（算子仓常用） |

两者的桥梁是：`TilingDef::SaveToBuffer` 拍平后的字节流，可以放进 `gert::TilingData` 的数据区传递。

## 4. 核心概念与源码讲解

### 4.1 gert::TilingData：追加式字节流容器

#### 4.1.1 概念说明

`gert::TilingData` 是一个「头部 + 连续数据区」的 POD 容器。头部记录三个关键值：容量（capacity）、已用长度（data_size）、数据区指针（data）。数据区紧跟头部之后分配，但 `TilingData` 本身**不拥有**这块内存——它由 `CreateCap` 工厂函数一次性分配，由调用方（通常是一个 `unique_ptr<uint8_t[]>`）管理生命周期。

它不认识任何字段语义，只提供最原始的「往后追加 N 字节」能力。字段解释完全靠写入方和读取方约定相同的结构体布局——这正是它能跨 host/device 传递的原因。

#### 4.1.2 核心流程

创建并写入一个 TilingData 的完整流程：

```text
1. CreateCap(cap_size)
   ├── 计算 total_size = sizeof(TilingData) + cap_size（含溢出检查）
   ├── new uint8_t[total_size]()  （值初始化，全零）
   └── 在头部调 Init：capacity_ = cap_size, data_size_ = 0,
                      data_ = 缓冲区首地址 + sizeof(TilingData)
2. Append(x)（可多次）
   ├── Expand(sizeof(x))：检查 data_size_ + sizeof(x) 是否溢出/超容量
   │     └── 超容量返回 nullptr → Append 返回 GRAPH_FAILED（本次追加不生效）
   └── 把 x 逐字节拷到 data_ + data_size_ 处，data_size_ += sizeof(x)
3. 读取方：GetData() 拿到数据区首地址，按约定布局 reinterpret_cast 解释
```

已用长度的恒等式：任意时刻数据区前 `GetDataSize()` 字节是有效内容，之后是未使用的预留空间。

#### 4.1.3 源码精读

**（1）类的成员布局——一个 64 字节的 POD 头部**

[inc/external/exe_graph/runtime/tiling_data.h:201-207](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L201-L207)：三个数据成员 `capacity_`、`data_size_`、`data_`，外加 40 字节 `reserved_` 保留区。文件末尾 [tiling_data.h:221](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L221) 用 `static_assert(std::is_standard_layout<TilingData>::value, ...)` 把「必须是 POD」固化为编译期约束——这是 metadef 一贯的 ABI 纪律（对照 u3-l1 的 KernelContext）。保留区的作用是给未来扩展留余量而不改变结构体大小，注释明确提醒「只剩 8 字节时不要直接使用」。

**（2）CreateCap——一次分配、头体连续**

[inc/external/exe_graph/runtime/tiling_data.h:157-169](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L157-L169)：先 `AddOverflow` 检查总大小，再 `new (std::nothrow) uint8_t[total_size]()` 分配（值初始化保证数据区清零），然后把 `data_` 指向「自己尾部之后」的位置。返回的是 `unique_ptr<uint8_t[]>`，调用方用 `reinterpret_cast<TilingData *>` 换视图使用——与 u3-l2 讲的「框架在裸内存上构造上下文」是同一套手法。

**（3）Expand——唯一的扩容原语**

[inc/external/exe_graph/runtime/tiling_data.h:139-150](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L139-L150)：先做加法溢出检查，再比较容量；两个检查任一失败都返回 `nullptr` 且**不改动 `data_size_`**（失败无副作用）。成功时返回扩展区首地址并推进 `data_size_`。注意这里没有 realloc、没有二次分配——容量在 `CreateCap` 时就锁死了。

**（4）两个 Append 重载——单值与数组**

[inc/external/exe_graph/runtime/tiling_data.h:109-117](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L109-L117)：单值版本用 `enable_if<is_standard_layout<T>>` 限定只接受 POD 类型，写入用 `*reinterpret_cast<T *>(data_ptr) = data`（placement 赋值而非 memcpy，因为是已对齐的定长写入）。

[tiling_data.h:119-132](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L119-L132)：数组版本先 `MulOverflow` 算总字节数，再走 `Expand`，最后 `memcpy`。注释说明：Expand 已保证合法性，此处直接 memcpy 减少冗余判断。

**（5）operator<<——流式语法糖**

[tiling_data.h:216-220](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L216-L220)：`td << a << b << c` 式的链式追加。注释坦诚说明取舍：因为不能抛异常，`operator<<` 无法把失败信息传给调用者（返回值被丢弃），所以它只适合「容量必然充足」的场合；需要检查失败时应显式调 `Append` 并判断返回值。

#### 4.1.4 代码实践

**实践目标**：验证 Append 的「追加无副作用失败」语义。

**操作步骤**：

1. 打开 [tests/ut/base/testcase/tiling_data_unittest.cc:120-139](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc#L120-L139)（`AppendOutOfBounds` 用例），阅读它做了什么：`CreateCap(20)` 只留 20 字节容量，先成功 Append 两个 int64（16 字节），第三次 Append 必然失败，断言返回值 `!= GRAPH_SUCCESS` 且 `GetDataSize()` 停在 16、前 16 字节内容未被破坏。
2. 在 `tests/ut/base/testcase/` 下新建一个测试文件（示例代码，非项目原有文件）：

```cpp
// my_tiling_data_overflow_ut.cc（示例代码）
#include "exe_graph/runtime/tiling_data.h"
#include <gtest/gtest.h>

namespace gert {
class MyTilingDataOverflowUT : public testing::Test {};

TEST_F(MyTilingDataOverflowUT, FailedAppendKeepsSize) {
  auto data = TilingData::CreateCap(12);  // 容量 12 字节
  auto td = reinterpret_cast<TilingData *>(data.get());
  int32_t a = 1;
  int32_t b = 2;
  int64_t c = 3;                          // 8 字节，塞不下
  EXPECT_EQ(td->Append(a), ge::GRAPH_SUCCESS);
  EXPECT_EQ(td->Append(b), ge::GRAPH_SUCCESS);
  EXPECT_NE(td->Append(c), ge::GRAPH_SUCCESS);  // 失败
  EXPECT_EQ(td->GetDataSize(), 8U);             // 长度停在 8，不回滚也不推进
}
}  // namespace gert
```

3. 运行（u1-l2 已讲过，`ut_metadef` 目标用 glob 自动收集 `tests/ut/base/testcase/*.cc`，新文件无需改 CMake）：

```bash
bash tests/run_test.sh -u
# 或在构建目录中精确过滤：
# ./build_gcov/ut/metadef/ut_metadef --gtest_filter=MyTilingDataOverflowUT.*
```

**需要观察的现象**：三个断言全部通过，特别是最后一个——失败的 Append 没有污染数据区长度。

**预期结果**：测试 PASSED。若无本地编译环境，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`CreateCap(0)` 之后立刻 `Append(int8_t(1))`，返回什么？`GetDataSize()` 是多少？

**答案**：`Expand(1)` 中 `after_size = 0 + 1 > capacity_ = 0`，返回 nullptr，因此 Append 返回 `GRAPH_FAILED`；`data_size_` 保持 0。合法但「什么都不装」的空容器。

**练习 2**：为什么 `Append` 的单值版本用 `*reinterpret_cast<T*>(...) = data` 而数组版本用 `memcpy`？

**答案**：单值写入的目标地址是自然对齐的定长槽位（容器头部本身按 8 字节成员对齐，数据区起点对齐，且 POD 追加顺序中各字段的对齐由写入方保证），直接赋值让编译器生成最优指令；数组版本源地址（调用方的 vector data）类型可能与目标解释不同、长度可变，语义上就是字节搬运，用 `memcpy` 最直接。两者都以 `Expand` 预先做过容量/溢出检查为前提。

**练习 3**：`operator<<` 连续追加失败时会发生什么？

**答案**：失败被静默忽略（注释原文：`we cannot throw exception, so callers cannot get the error information`），后续内容会继续从当前 `data_size_` 处追加，产生**不完整但自洽**的字节流。所以写 device 侧消费的 tiling data 时应优先用带返回值检查的 `Append`。

### 4.2 TilingContext 与 TilingData 的衔接

#### 4.2.1 概念说明

u3-l3 讲过：TilingFunc 通过 `TilingContext` 拿到 tiling data 输出槽位。这里补上最后一块拼图——`gert::TilingData` 容器就装在那个槽位里，且框架提供了两种取用风格：

- **覆写式**（`GetTilingData<T>()`）：把容器当成「一个 T 的存储空间」用，一次性写入整个结构体。
- **追加式**（`GetRawTilingData()`）：拿到裸 `TilingData*`，用 `Append` 一段段拼。

#### 4.2.2 核心流程

```text
TilingFunc 内部：
  方式 A（覆写式，适合简单 tiling 参数）
    T *td = context->GetTilingData<T>();   // 检查容量 ≥ sizeof(T)，
    td->field = value;                      // SetDataSize(sizeof(T))，
                                           // 返回数据区指针，直接按结构体赋值
  方式 B（追加式，适合变长/分段内容）
    TilingData *raw = context->GetRawTilingData();
    raw->Append(a); raw->Append(b); raw->Append(arr, n);
```

#### 4.2.3 源码精读

[inc/external/exe_graph/runtime/tiling_context.h:394-405](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L394-L405)：`GetTilingData<T>()` 先取裸容器，做两层防御——空指针检查和容量检查（`GetCapacity() < sizeof(T)` 则返回 nullptr，因为编译结果里该算子允许的最大 tiling data 长度是编译期定死的）。通过后 `SetDataSize(sizeof(T))` 登记长度，再把数据区 `static_cast<T*>` 返回。注意它**不做任何运行期类型核对**：T 是什么完全由算子作者与 kernel 侧的约定决定，写错 T 就是纯粹的内存误解释（u3-l3 已提示过这一点）。

[inc/external/exe_graph/runtime/tiling_context.h:410-412](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L410-L412)：`GetRawTilingData()` 只有一行——从 `kOutputTilingData` 槽位取出 `TilingData*`，失败返回空指针。这是「槽位下标公式 + 指针类型解释」模式（u3-l3 的核心结论）在本讲的直接体现。

#### 4.2.4 代码实践

**实践目标**：对照单测确认覆写式与追加式可以混用同一条读取路径。

**操作步骤**：

1. 阅读 [tests/ut/base/testcase/tiling_data_unittest.cc:27-47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc#L27-L47) 的 `BuildTestContext()`：它用 `OpTilingContextBuilder`（u5-l1 会详讲）搭出一个带 9 个属性的假 TilingContext，后续大量用例都从它的 `context->GetAttrs()` 取属性来做转换测试。
2. 阅读 [tiling_data_unittest.cc:50-61](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc#L50-L61)（`AppendSameTypesOk`）：连追 10 个 int64 后 `GetDataSize()==80`，并用 `memcmp(GetData(), expect_vec.data(), ...)` 验证字节流内容——这就是「读取方按写入顺序解释」的最小示范。

**需要观察的现象 / 预期结果**：`memcmp` 为 0，说明追加顺序与内存布局严格一致。本步骤为纯阅读实践，无需运行（如运行见 4.4 综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：`GetTilingData<T>()` 为什么在返回前就 `SetDataSize(sizeof(T))`，而不是留给算子作者设置？

**答案**：框架在 kernel 启动时要按 `GetDataSize()` 把有效字节流搬到 device。覆写式语义下有效长度就是 `sizeof(T)`，提前登记可以避免作者忘记设置导致长度为 0（device 侧拿到空数据）。追加式则必须由作者通过逐次 `Append` 自然推进长度。

**练习 2**：如果 TilingFunc 里先 `GetTilingData<T>()` 又 `GetRawTilingData()->Append(x)`，最终 data_size 是多少？

**答案**：`sizeof(T) + sizeof(x)` 的字节数——覆写式把长度设为 `sizeof(T)`，其后的 Append 在这个基础上继续追加。但要注意 T 的尾部若有对齐填充，追加内容会紧贴 `sizeof(T)` 处开始，混合使用需自己保证解释约定一致。

### 4.3 AppendConvertedAttrVal：属性的类型化搬运

#### 4.3.1 概念说明

很多算子的 tiling 参数直接来自算子属性（如 `axis`、`epsilon`），但属性在 RuntimeAttrs 里的存储类型与 kernel 期望的 tiling 类型可能不同（例如属性是 float32，device 侧只认 float16）。`TilingData::AppendConvertedAttrVal` 把「取属性 → 类型转换 → 追加」三步合成一次调用，转换规则集中注册在一张编译期构造的二维表里。

#### 4.3.2 核心流程

```text
AppendConvertedAttrVal(attrs, index, src_type, dst_type)
  ├── attrs == nullptr                → GRAPH_FAILED
  ├── index >= attrs->GetAttrNum()    → GRAPH_FAILED（越界）
  ├── kAttrTable.Find(src, dst)       → 查二维表
  │     └── 未注册的组合（如 string→int32）→ GRAPH_FAILED
  └── func(this, attrs, index)        → 执行具体的 取值/转换/Append
```

#### 4.3.3 源码精读

[base/runtime/tiling_data.cc:597-613](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L597-L613)：入口的三段防御 + 查表分发。表本体是 [tiling_data.cc:445-593](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L445-L593) 的 `kAttrTable`：一个 `AttrTable<kTypeEnd, kTypeEnd>` 的常量表，用链式 `.Add(src, dst, func)` 注册了 bool/float32/int32/int64 及其 list、list-list 形态到各种目标类型的转换函数。类型枚举 `AttrDataType` 定义在 [tiling_data.h:25-65](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L25-L65)，取值顺序是接口契约，只能尾部追加（与 u2-l3 ValueType 的纪律一致）。

代表性转换函数：

- [tiling_data.cc:51-57](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L51-L57) `AppendAttr<T>`：同类型直通的模板——取属性指针、判空、`Append`。
- [tiling_data.cc:85-95](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L85-L95) `AppendConvertedAttr<T1, T2>`：数值类型间的 `static_cast` 转换，转换前用 `IntegerChecker<T2>::Compat` 检查收窄溢出（仅告警不阻断）。
- [tiling_data.cc:151-156](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L151-L156) `AppendStrAttr`：字符串属性按 `strlen` 追加裸字节（不含终止符 `\0`——见 4.3.5 练习 2）。
- 列表类型（如 [tiling_data.cc:61-66](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L61-L66) `AppendListAttr`）：从 `ContinuousVector` 取元素区，走数组版 `Append<T>(ptr, n)`。

容量安全由 [tiling_data.cc:31-48](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L31-L48) 的 `CheckOverFlow<T>` 统一兜底：乘法溢出、加法溢出、超容量三查全失败才放行。

#### 4.3.4 代码实践

**实践目标**：通过单测确认「未注册的组合会失败」这一边界行为。

**操作步骤**：

1. 阅读 [tests/ut/base/testcase/tiling_data_unittest.cc:1964-1974](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc#L1964-L1974)（`AppendAttrSrcTypeInvalid`）：对同一个 bool 属性，声称 src 是 `kString` 或 `kListInt64` 都会因查表失败返回 `GRAPH_FAILED`。
2. 对照 [tiling_data.cc:448-593](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L448-L593) 的 `.Add` 清单，找出哪些目标类型**从未**出现在表中（例如 `kString` 只能作为目标出现一次：`kString→kString`）。

**需要观察的现象**：`kAttrTable` 中 string 只有一条直通路径；所有以 `kString` 为源的其他组合都是 `Find` 返回 nullptr。

**预期结果**：与单测断言一致。源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：属性是 `list_list_int32`，目标 `kListListInt64`，追加后 `GetDataSize()` 增加多少？

**答案**：所有内层 list 元素总数 × 8 字节。`AppendConvertedListListAttr`（[tiling_data.cc:121-148](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L121-L148)）把二层结构**拍平**成一个连续的 int64 序列，不保留外层长度信息——外层有几个 list、每个多长，需要读取方另行约定。

**练习 2**：`AppendStrAttr` 追加 "Hello!"（6 字符）后 `GetDataSize()` 是 6 还是 7？

**答案**：6。见 [tiling_data_unittest.cc:141-154](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc#L141-L154) 的断言：`Append(attr, strlen(attr))` 不含 `\0`。device 侧如果要当 C 字符串用，必须自己补终止符或另行传长度。

### 4.4 tilingdata_base.h 宏体系：结构化的 TilingDef

#### 4.4.1 概念说明

`gert::TilingData` 是「无类型的字节管道」，直接用它写复杂 tiling 参数很繁琐。`optiling` 命名空间的宏体系在字节管道之上提供了一层**结构化封装**：你用宏声明字段，宏在编译期为你生成一个类，它同时具备：

1. 每个字段的 `set_xxx()/get_xxx()` 访问器；
2. 自动的对齐占位（补齐字节），保证字段偏移确定；
3. 一份「字段元信息」（类型名、字段名、偏移）供上层工具序列化/校验；
4. 通过 `REGISTER_TILING_DATA_CLASS` 宏把「算子名 → 构造函数」登记进全局工厂，使框架能按算子名从字节流重建结构体（反序列化的关键）。

#### 4.4.2 核心流程

以头文件注释中的官方示例（[tilingdata_base.h:135-145](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L135-L145)）为准：

```cpp
BEGIN_TILING_DATA_DEF(MaxPoolTilingData)
    TILING_DATA_FIELD_DEF(int32_t, dim_0);
    TILING_DATA_FIELD_DEF(uint8_t, var_1);
    TILING_DATA_FIELD_DEF(int64_t, factor_1);
END_TILING_DATA_DEF
REGISTER_TILING_DATA_CLASS(MaxPool, MaxPoolTilingData)
```

展开后的生命周期：

```text
构造（host 侧，TilingFunc 中）
  1. 每个字段的偏移在成员初始化时由 FieldHandler 登记：
       field_info_ += FieldInfo(dtype, name)；data_size_ += sizeof(type)
  2. CheckAlignAndGenPlaceHolder：若当前 data_size_ 不是下一字段对齐倍数，
     自动插入 uint8_t 占位数组字段补齐
  3. 构造函数末尾 InitData()：new uint8_t[data_size_]() 分配连续数据区，
     嵌套 struct 字段通过 saveBufferPtr 递归挂接子数据区
  4. set_dim_0(16)：同时更新宿主侧副本和 data_ptr_ + offset_ 处的字节流

序列化
  SaveToBuffer(buf, cap)：memcpy_s 把 data_ptr_ 前 data_size_ 字节拷给调用方
  （若曾 SetDataPtr 外部内存则跳过——数据已在目标缓冲区里）

反构造（按算子名从字节流重建）
  CTilingDataClassFactory::CreateTilingDataInstance("MaxPool")
    → 查 map 得到构造函数 → new MaxPoolTilingData()
  再 SetDataPtr(外部字节流地址)：不拷贝数据，让字段访问器直接
  解释外部内存（零拷贝视图），并递归重挂嵌套 struct
```

#### 4.4.3 源码精读

**（1）TILING_DATA_FIELD_DEF——一行声明生成四个成员**

[inc/external/register/tilingdata_base.h:191-204](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L191-L204)：宏为每个字段生成 `set_x`（写宿主副本 + 写字节流两处）、`get_x`（只读宿主副本）、私有成员「值副本 + 偏移量 + 16 字节保留缓冲」。其中最巧妙的是偏移量的取得方式：成员初始化 `size_t field_name##_offset_ = FieldHandler(...)` 在构造时按声明顺序执行，`FieldHandler`（[tilingdata_base.h:149-155](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L149-L155)）返回追加前的 `data_size_` 并推进总长——用成员初始化顺序天然实现了「字段布局即声明顺序」。16 字节 `reserve_buf_` 为将来给字段附加元信息预留，不改变现有布局。

**（2）对齐占位——为什么需要 PH 字段**

[base/asc/tilingdata_base_impl.cc:91-99](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc#L91-L99)：`CheckAlignAndGenPlaceHolder` 在每个字段登记前检查当前 `data_size_` 能否被该字段类型大小整除，不能则插入一段 uint8_t 占位数组。例如 `dim_0`(int32,4B) 之后接 `factor_1`(int64,8B) 时，`data_size_=5` 不是 8 的倍数，自动补 3 字节 PH。这保证字节流中每个字段的偏移与宿主结构体的自然对齐一致，是「直接按偏移强转解释」安全的前提。

**（3）InitData / SetDataPtr——两种数据区来源**

[base/asc/tilingdata_base_impl.cc:101-119](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc#L101-L119)：默认构造路径 `new` 一块自有数据区，并遍历 `saveBufferPtr` 把嵌套 struct 字段的数据区指到主数据区的对应偏移处（嵌套 struct 因此不单独分配）。

[base/asc/tilingdata_base_impl.cc:59-71](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc#L59-L71)：`SetDataPtr` 是反构造路径——释放自有数据区后改为指向**外部字节流**，置 `inited_data_ptr=true` 标记所有权转移（析构函数 [tilingdata_base.h:72-78](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L72-L78) 据此决定是否 `delete[]`）。此后所有 `set_x/get_x` 直接读写外部内存，零拷贝。

**（4）SaveToBuffer 与工厂**

[base/asc/tilingdata_base_impl.cc:73-89](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc#L73-L89)：序列化就是一次带容量检查的 `memcpy_s`；若已 `SetDataPtr` 过则直接返回（数据已在目标处）。

[base/asc/tilingdata_base_impl.cc:161-186](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc#L161-L186)：工厂用 `map<op_type, 构造函数>` 存储；查不到或构造函数为空都返回 nullptr（空值失败语义，不抛异常）。注册入口是 [tilingdata_base.h:241-255](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L241-L255) 的 `REGISTER_TILING_DATA_CLASS` 宏——它生成一个匿名命名空间的 helper 类，靠**静态全局对象的构造函数**在 so 加载时完成注册，与 u4-l3 将讲的 OpDefRegistry 宏是同一模式，可提前对照。

另外两个辅助机制：[tilingdata_base.h:29-51](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L29-L51) 的 `StructSizeInfoBase` 单例记录「struct 类名 → 已登记大小」，供 `TILING_DATA_FIELD_DEF_STRUCT`（[tilingdata_base.h:227-235](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L227-L235)）嵌套组合时查询子结构大小；[base/asc/tilingdata_base_impl.cc:211-229](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc#L211-L229) 的 `RecordTilingStruct` 用 weak 函数在加载期检测不同头文件中同名 tiling struct 的冲突并 printf 告警。

#### 4.4.4 代码实践

**实践目标**：亲手构造一个含两个 int32 字段的结构化 TilingDef，完成「定义 → 赋值 → 拍平 → 从字节流零拷贝反构造 → 断言一致」全链路（本讲的核心实践任务）。

**操作步骤**：

1. 在 `tests/ut/base/testcase/` 下新建 `my_tilingdef_roundtrip_ut.cc`（示例代码，非项目原有文件）：

```cpp
// my_tilingdef_roundtrip_ut.cc（示例代码）
#include "register/tilingdata_base.h"
#include <gtest/gtest.h>

namespace optiling {
// 1) 用宏定义一个两个 int32 字段的 tiling 结构
BEGIN_TILING_DATA_DEF(MyAddTilingData)
    TILING_DATA_FIELD_DEF(int32_t, block_dim);
    TILING_DATA_FIELD_DEF(int32_t, tile_num);
END_TILING_DATA_DEF

class MyTilingDefRoundTripUT : public testing::Test {};

TEST_F(MyTilingDefRoundTripUT, SaveAndReconstruct) {
  // 2) host 侧构造并赋值
  MyAddTilingData td;
  td.set_block_dim(8);
  td.set_tile_num(128);
  ASSERT_EQ(td.GetDataSize(), sizeof(int32_t) * 2U);

  // 3) 拍平成字节流（模拟跨进程传递）
  uint8_t buf[64] = {0};
  td.SaveToBuffer(buf, sizeof(buf));

  // 4) 从字节流零拷贝反构造：工厂按 op_type 新建，再 SetDataPtr 指向 buf
  auto reborn = CTilingDataClassFactory::GetInstance().CreateTilingDataInstance("NoSuchOp");
  EXPECT_EQ(reborn, nullptr);  // 未注册的算子名应返回空

  MyAddTilingData view(buf);   // 第二种反构造方式：带指针构造，直接解释外部内存
  EXPECT_EQ(view.get_block_dim(), 8);
  EXPECT_EQ(view.get_tile_num(), 128);
  EXPECT_EQ(*reinterpret_cast<int32_t *>(buf), 8);          // 字节流前 4 字节
  EXPECT_EQ(*reinterpret_cast<int32_t *>(buf + 4), 128);    // 后 4 字节
}
}  // namespace optiling
```

说明：示例中未调用 `REGISTER_TILING_DATA_CLASS`（它会向全局单例注册，测试进程内重复注册同名算子可能互相干扰），因此工厂查询用「未注册返回 nullptr」来验证查表失败路径；正构造→反构造的一致性用「带指针构造函数」验证——[tilingdata_base.h:181-189](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L181-L189) 的 `explicit class_name(void *ptr)` 重载正是为此设计。

2. 运行：

```bash
bash tests/run_test.sh -u
```

**需要观察的现象**：所有断言通过；`GetDataSize()` 恰为 8（两个 int32 无需占位补齐）；从 `buf` 直接强转读出的两个 int32 与写入值一致。

**预期结果**：测试 PASSED。若无本地编译环境，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：把字段改成 `int32_t a; int8_t b; int64_t c;`，`GetDataSize()` 是多少？

**答案**：17。a 占 0–3，b 占 4；登记 c 前发现 `data_size_=5` 不是 8 的倍数，`CheckAlignAndGenPlaceHolder` 插入 3 字节 PH（偏移 5–7），c 占 8–15，共 16 字节——再加每个 TilingDef 构造时开头为类名 PH 预留的对齐（构造函数里 `CheckAlignAndGenPlaceHolder(#class_name "PH", 8)` 在**空数据区**上执行，`0 % 8 == 0` 故不补字节），最终 `data_size_` 为 16。若你的实测结果与此不同，请以 `GetFieldInfo()` 打印的实际占位为准（待本地验证：不同对齐路径取决于字段声明顺序）。

**练习 2**：`SetDataPtr` 之后析构函数为什么不能 `delete[] data_ptr_`？

**答案**：`SetDataPtr` 把 `inited_data_ptr` 置 true，标记数据区是**外部内存**（可能指向 gert::TilingData 的数据区、或调用方栈缓冲），所有权不属于本对象。析构函数 [tilingdata_base.h:72-78](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L72-L78) 只在 `!inited_data_ptr` 时释放——一个用 bool 模拟的简易所有权模型。

**练习 3**：宏体系与 `gert::TilingData` 各自的 `GetDataSize()` 含义有何不同？

**答案**：`TilingDef::GetDataSize()` 是**结构布局总长**（含占位字段，构造即固定，等于序列化后的字节数）；`gert::TilingData::GetDataSize()` 是**已写入长度**（随 Append 增长）。前者是「这份 tiling 定义拍平后多大」，后者是「这根管道里现在装了多少」。

## 5. 综合实践

把本讲三个模块串起来，完成一个「属性 → 转换追加 → 覆写式写入 → 读回断言」的端到端用例（示例代码，作为新测试文件放入 `tests/ut/base/testcase/`）：

```cpp
// my_tiling_e2e_ut.cc（示例代码）
#include "exe_graph/runtime/tiling_data.h"
#include "base/context_builder/op_kernel_run_context_builder.h"
#include "base/context_builder/op_tiling_context_builder.h"
#include "common/ge_common/debug/ge_log.h"
#include <gtest/gtest.h>

namespace gert {
struct MyTiling {   // 覆写式布局：两个 int32 字段
  int32_t block_dim;
  int32_t tile_num;
};

class MyTilingE2EUT : public testing::Test {};

TEST_F(MyTilingE2EUT, OverwriteThenAppend) {
  // A. 覆写式：模拟 GetTilingData<T>() 的内部动作（不依赖真实 context）
  auto buf = TilingData::CreateCap(64);
  auto td = reinterpret_cast<TilingData *>(buf.get());
  ASSERT_GE(td->GetCapacity(), sizeof(MyTiling));
  td->SetDataSize(sizeof(MyTiling));
  auto t = static_cast<MyTiling *>(td->GetData());
  t->block_dim = 4;
  t->tile_num = 64;

  // B. 追加式：在其后追加一个 int32 尾部字段
  int32_t tail = 7;
  EXPECT_EQ(td->Append(tail), ge::GRAPH_SUCCESS);
  ASSERT_EQ(td->GetDataSize(), sizeof(MyTiling) + sizeof(int32_t));

  // C. 读回：按写入约定逐段解释字节流
  auto bytes = reinterpret_cast<const uint8_t *>(td->GetData());
  EXPECT_EQ(*reinterpret_cast<const int32_t *>(bytes), 4);
  EXPECT_EQ(*reinterpret_cast<const int32_t *>(bytes + 4), 64);
  EXPECT_EQ(*reinterpret_cast<const int32_t *>(bytes + 8), 7);

  // D. 越界防护：容量 64，已用 12，一次追加 64 字节应失败
  const char big[64] = {0};
  EXPECT_NE(td->Append(big, sizeof(big)), ge::GRAPH_SUCCESS);
  EXPECT_EQ(td->GetDataSize(), 12U);  // 失败不推进长度
}
}  // namespace gert
```

运行方式：`bash tests/run_test.sh -u`，用 `--gtest_filter=MyTilingE2EUT.*` 聚焦观察。这个用例覆盖了 4.1 的追加与失败语义、4.2 的覆写式长度登记逻辑、以及读取方的布局解释约定——正是 TilingFunc 与 device kernel 之间字节流契约的微缩模型。若想进一步接入真实 TilingContext，可仿照 [tiling_data_unittest.cc:27-47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc#L27-L47) 用 `OpTilingContextBuilder` 构建上下文后调 `AppendConvertedAttrVal`（其 Builder 细节在 u5-l1 展开）。无本地环境时标注「待本地验证」。

## 6. 本讲小结

- `gert::TilingData` 是 64 字节 POD 头部 + 连续数据区的追加式字节流容器，容量在 `CreateCap` 时锁死，`Expand` 失败无副作用，整类被 `static_assert(is_standard_layout)` 固化 ABI 契约。
- `TilingContext` 提供两种写入风格：覆写式 `GetTilingData<T>()`（登记 `sizeof(T)` 长度、直接按结构体赋值）与追加式 `GetRawTilingData()`（逐段 `Append`），读取方靠共同的结构布局约定解释字节流，无运行期类型核对。
- `AppendConvertedAttrVal` 用 (源类型, 目标类型) 二维函数表把算子属性转换后搬运进容器，未注册的组合、越界索引一律返回 `GRAPH_FAILED`。
- `optiling` 宏体系（`BEGIN_TILING_DATA_DEF` 等）在字节流之上提供结构化封装：字段偏移由成员初始化顺序天然决定、对齐由自动 PH 占位保证、序列化是 `SaveToBuffer` 的一次 memcpy、反构造靠工厂按算子名新建 + `SetDataPtr` 零拷贝挂接外部内存。
- 两套体系的分工：`gert::TilingData` 是运行时容器（gert 新体系），`optiling::TilingDef` 是宿主侧结构化定义（老注册体系），`SaveToBuffer` 的产物可流入前者传递。
- 跨 host/device 传递的一切前提是 POD、无指针、无 STL 布局依赖——与 metadef 全仓的 ABI 纪律一脉相承。

## 7. 下一步学习建议

本讲是单元三（exe_graph 运行时上下文）的收官。接下来建议：

- 进入单元四，先读 [u4-l1 register 模块总览](u4-l1-register-overview.md)：本讲 4.4 出现的「静态对象构造期注册」模式将在算子注册链路中大规模复用。
- 若想先补齐上下文构建侧的拼图，可跳读 [u5-l1 ContextBuilder 体系](u5-l1-context-builder.md)：本讲单测中反复出现的 `OpTilingContextBuilder` 的内部机制在那里详解。
- 源码延伸阅读：`inc/external/exe_graph/runtime/continuous_vector.h`（列表属性的数据载体，4.3 中 `AppendListAttr` 的取数来源）与 `inc/common/util/tiling_utils.h`（float16/bfloat16 转换函数所在）。
