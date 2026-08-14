# TilingData：tiling 参数的序列化与传递

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `gert::TilingData` 容器的内存布局（头部 + 紧随其后的字节流）以及它为什么必须是 POD。
2. 掌握 `Append` / `Expand` / `CreateCap` 的字段追加与容量控制机制，理解溢出防护的实现。
3. 理解 `AppendConvertedAttrVal` 如何借助 `AttrDataType` 枚举和函数指针表把上下文属性转换后追加进 tiling 字节流。
4. 读懂 `tilingdata_base.h` 中 `BEGIN_TILING_DATA_DEF` 宏族背后的 `optiling::TilingDef` 基类与工厂注册机制，理解「带类型的 tiling 结构体」与「无类型的字节流容器」两套体系的关系。
5. 能参照现有单测，自己动手写一个序列化/反序列化的小测试并跑通。

## 2. 前置知识

**什么是 tiling？** 在昇腾硬件上执行算子前，框架需要把输入切分成硬件友好的分块（tile），并把分块参数（每个维度切多少、块数、步长等）告诉设备侧的 kernel 代码。这个「计算切分方案」的阶段叫 tiling，其产出的参数集合叫 **tiling data**。

**为什么需要序列化？** tiling 阶段在**宿主侧**（CPU）执行，kernel 在**设备侧**（NPU）执行，两者之间只能传递一块连续的原始内存。因此 tiling 结果必须被打平成一串字节（POD 字节流），设备侧再按相同的结构布局把这串字节解释回来。这决定了两个约束：

- 字节流里只能放 **standard layout**（标准布局）类型——布局必须在不同编译单元之间完全一致；
- 追加和读取都不能抛异常，只能用返回值报错（承接 [u3-l1](u3-l1-kernel-context.md) 讲过的「失败返回空值而非异常」语义）。

**本仓里有两个名字相近的东西，先区分开：**

| 名字 | 命名空间 | 角色 |
| --- | --- | --- |
| `gert::TilingData`（`exe_graph/runtime/tiling_data.h`） | gert | **无类型字节流容器**：头部（容量/长度/指针）+ 紧随其后的数据区，框架与算子之间的传输载体 |
| `optiling::TilingDef` 派生类（`register/tilingdata_base.h`） | optiling | **带类型的结构体定义**：用宏声明字段（`set_xxx`/`get_xxx`），最终也序列化成字节流（`SaveToBuffer`），供算子仓使用 |

一句话：`gert::TilingData` 管「装字节」，`optiling::TilingDef` 宏体系管「按字段名读写字节」。两者最终打平后的字节流是同一种东西。

还需要回忆两个前置概念（来自 [u3-l3](u3-l3-tiling-context.md)）：

- **TilingContext 输出槽位**：tiling 结果统一写到 `TilingOutputIndex` 枚举定义的输出槽，其中 `kOutputTilingData` 槽放的就是本讲的 `TilingData` 容器。
- **`static_assert(std::is_standard_layout<T>::value)`**：把「结构布局不可变」固化为编译期检查，是 metadef 的 ABI 守护手段（详见 [u5-l4](u5-l4-abi-compatibility.md)）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [inc/external/exe_graph/runtime/tiling_data.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h) | `gert::TilingData` 容器与 `AttrDataType` 枚举，全头文件实现（inline/模板） |
| [base/runtime/tiling_data.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc) | `AppendConvertedAttrVal` 的实现：类型转换追加函数族 + 函数指针查找表 |
| [inc/external/register/tilingdata_base.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h) | `optiling::TilingDef` 基类、字段定义宏族、tiling 结构体工厂 |
| [base/asc/tilingdata_base_impl.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc) | `TilingDef` 各方法与工厂的实现（`InitData`/`SaveToBuffer`/`SetDataPtr` 等） |
| [inc/external/exe_graph/runtime/tiling_context.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h) | `GetTilingData<T>()` / `GetRawTilingData()`：容器与 Tiling 阶段的衔接点 |
| [tests/ut/base/testcase/tiling_data_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc) | 本讲的实践参照：容器的追加/越界/属性转换测试 |

## 4. 核心概念与源码讲解

### 4.1 gert::TilingData 容器：头体分离的字节流缓冲区

#### 4.1.1 概念说明

`gert::TilingData` 解决的问题是：**用一块裸内存同时携带「元信息（容量、已写长度）」和「数据本身」**。它采用「头部 + 尾随数据区」的变长结构：对象头部记录容量和长度，数据区紧跟在头部之后。这样框架只需保存一个指针，就能把整块内存搬到设备侧，设备侧读同一个头部即可知道有效数据有多长。

这个设计和 [u2-l4](u2-l4-shape-stride-tensor.md) 讲过的 `TensorData` 的 `kFollowing` placement（头体连续分配）如出一辙——都是为了避免二次分配、让数据可整体搬运。

#### 4.1.2 核心流程

```text
CreateCap(cap)                          Init(cap, buf + sizeof(TilingData))
   │ 分配 sizeof(TilingData)+cap 字节        │ capacity_ = cap
   │ 的连续内存并清零                         │ data_size_ = 0
   ▼                                        │ data_ 指向头部之后
┌─────────────────────┬────────────────────────────────┐
│ 头部（POD，64 字节）  │ 数据区（cap 字节，追加写于此）      │
│ capacity_ data_size_ │  [字段1][字段2][字段3]...          │
│ data_ reserved_[40]  │  ◄─ data_ 指向这里                 │
└─────────────────────┴────────────────────────────────┘

Append(x)：Expand(sizeof(x)) 拿到写入地址 → 按类型写入 → data_size_ += sizeof(x)
```

追加长度恒有：

\[ \text{data\_size\_{after}} = \text{data\_size\_{before}} + \text{sizeof}(T) \le \text{capacity\_} \]

#### 4.1.3 源码精读

成员布局：三个有效字段加 40 字节保留区，文件末尾用 `static_assert` 固化 POD 契约。

- [inc/external/exe_graph/runtime/tiling_data.h:201-207](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L201-L207)：`capacity_`（最大容量）、`data_size_`（已写长度）、`data_`（指向数据区的指针）、`reserved_[40]` 保留字段。保留区的注释说明这是为未来扩展预留的，不能直接使用。
- [inc/external/exe_graph/runtime/tiling_data.h:221](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L221)：`static_assert(std::is_standard_layout<TilingData>::value, ...)`，把「必须是标准布局」变成编译期错误，任何破坏布局的改动（比如加虚函数）都无法编译通过。
- [inc/external/exe_graph/runtime/tiling_data.h:196-199](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L196-L199)：拷贝与移动构造/赋值全部 `= delete`——容器本身只是内存头部视图，拷贝头部会丢数据区，所以干脆禁止。

工厂方法 `CreateCap`：一次性分配「头部 + 数据区」并完成初始化。

- [inc/external/exe_graph/runtime/tiling_data.h:157-169](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L157-L169)：`new (std::nothrow) uint8_t[total_size]()` 分配并清零整块内存，然后把首地址 `reinterpret_cast` 成 `TilingData*`，调用 `Init` 时让 `data_` 指向 `td_buf.get() + sizeof(TilingData)`——即头部之后紧跟的数据区。返回 `unique_ptr<uint8_t[]>` 由调用方管理生命周期（单测中 `data.get()` 再转回 `TilingData*` 用）。
- [inc/external/exe_graph/runtime/tiling_data.h:187-192](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L187-L192)：`Init` 设置容量、清零长度、让 `data_` 指向外部传入的地址，并把保留区 memset 清零。

与 Tiling 阶段的衔接：TilingContext 从 `kOutputTilingData` 输出槽取出容器。

- [inc/external/exe_graph/runtime/tiling_context.h:394-412](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L394-L412)：`GetTilingData<T>()` 先经 `GetRawTilingData()` 拿到容器指针，校验容量不小于 `sizeof(T)` 后，`SetDataSize(sizeof(T))` 登记长度并把 `GetData()` 强转成 `T*` 返回——这就是「把结构体覆写进字节流并登记长度」的入口；`GetRawTilingData()` 则返回无类型容器，配合 `Append` 使用。

#### 4.1.4 代码实践

**实践目标**：直观验证「头体分离」布局——`CreateCap` 分配的整块内存里，头部之后紧跟数据区，`sizeof(TilingData)` 恰好是头部偏移。

**操作步骤**（阅读型，不写文件）：

1. 打开 [tests/ut/base/testcase/tiling_data_unittest.cc:50-61](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc#L50-L61) 的 `AppendSameTypesOk` 用例，注意 `CreateCap(2048)` 返回 `unique_ptr<uint8_t[]>`，再 `reinterpret_cast<TilingData *>` 使用。
2. 追踪 `CreateCap` 里 `td->Init(cap_size, td_buf.get() + sizeof(TilingData))` 这一行（[tiling_data.h:167](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L167)），确认 `data_` 永远指向头部之后。
3. 回忆 [u1-l2](u1-l2-build-and-test.md)：`ut_metadef` 目标用 glob 收集 `tests/ut/base/testcase/*.cc`，所以该测试无需在 CMake 中登记即可被编译。

**需要观察的现象 / 预期结果**：`tiling_data->Append(i)` 循环追加 10 个 `int64_t` 后 `GetDataSize() == 80`，且 `memcmp(GetData(), expect_vec.data(), 80) == 0`——字节流内容与直接内存拷贝一致。运行 `bash tests/run_test.sh -u` 后用 `--gtest_filter=TilingDataUT.AppendSameTypesOk` 过滤可单跑此用例（完整运行方式见综合实践）。本实践为源码阅读型，命令执行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TilingData` 禁止拷贝构造，却还允许 `reinterpret_cast` 出「第二个」`TilingData*`？

**答案**：禁拷贝防止的是「按值复制头部」——拷出来的头部 `data_` 指向原对象的数据区，两个头都会以为自己拥有数据，析构/写入语义全乱。而 `reinterpret_cast` 只是把同一段内存换一个视角访问（和 [u3-l2](u3-l2-extended-context.md) 的上下文视图同一手法），不产生新头部、不涉及所有权，所以安全。

**练习 2**：`reserved_[40]` 保留区为什么不能「直接用」？

**答案**：头文件注释明确写了 `do not directly use when only 8-byte left`。这个类的布局是 ABI 契约（`static_assert` 只能保证 standard layout，不能保证字段不变）；已编译的框架/算子 so 按当前偏移访问 `capacity_`/`data_size_`/`data_`，若新版本改了保留区用法导致字段偏移变化，旧 so 会读写错位。保留区只用于在不移动既有字段的前提下做尾部扩展。

**练习 3**：`GetTilingData<T>()` 里为什么必须调用 `SetDataSize(sizeof(T))`？

**答案**：`data_size_` 是框架搬运字节流时的依据（搬多少字节）。覆写结构体只写了内存，不写 `data_size_` 的话框架仍认为是旧长度，设备侧会读到不完整或错误的 tiling 参数。`SetDataSize` 相当于「登记本次序列化的最终长度」。

### 4.2 Append 与 Expand：字段追加和溢出防护

#### 4.2.1 概念说明

`Append` 是序列化的唯一正规入口：把一个（或一段）standard layout 值追加到字节流末尾。所有长度算术都走 `ge::AddOverflow` / `ge::MulOverflow` 溢出检查函数（与 [u2-l1](u2-l1-datatype-and-format.md) 讲过的 `GetSizeInBytes` 溢出防护同一套工具），超出容量即失败返回，绝不越界写。

#### 4.2.2 核心流程

```text
Append(T data)                Expand(sizeof(T))
  │                             │ after_size = data_size_ + size（溢出则返回 nullptr）
  │                             │ after_size > capacity_ → 返回 nullptr
  │                             │ 返回 data_ + data_size_（旧末尾），data_size_ = after_size
  ▼                             ▼
Expand 成功 → *reinterpret_cast<T*>(ptr) = data → GRAPH_SUCCESS
Expand 失败 → 直接返回 GRAPH_FAILED（一个字节都没写）
```

注意 `Expand` 的语义细节：**先返回旧末尾地址、再更新长度**；失败时长度不变，因此一次失败的 Append 不会污染已有数据。

#### 4.2.3 源码精读

- [inc/external/exe_graph/runtime/tiling_data.h:109-117](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L109-L117)：单值 `Append`。模板参数带 `std::enable_if<std::is_standard_layout<T>::value>`——非标准布局类型（如含虚函数、非标准布局成员的类）直接编译失败，从源头杜绝布局不确定的类型进入字节流。
- [inc/external/exe_graph/runtime/tiling_data.h:119-132](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L119-L132)：数组版 `Append(const T *data, size_t append_num)`。先用 `MulOverflow` 算总字节数（防止 `个数 × sizeof` 溢出），再 `Expand`，最后 `memcpy`。注释说明：Expand 已保证合法，此处省去冗余检查直接 memcpy。乘法溢出与容量不足分别返回 `GRAPH_MUL_OVERFLOW` / `GRAPH_ADD_OVERFLOW` 两种错误码。
- [inc/external/exe_graph/runtime/tiling_data.h:139-150](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L139-L150)：`Expand` 本体。返回写入地址的计算是 `data_ + data_size_`（按 `uint8_t*` 步进），两道检查：加法溢出、超容量。
- [inc/external/exe_graph/runtime/tiling_data.h:216-220](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b174941997bc7d/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L216-L220)：流式 `operator<<`。注释坦承它「无法把错误抛给调用者」，失败被静默忽略——所以工程代码应优先用返回 `graphStatus` 的 `Append`。
- [tests/ut/base/testcase/tiling_data_unittest.cc:120-139](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc#L120-L139)：`AppendOutOfBounds` 用例验证「容量 20 字节 → 前 2 个 int64 写入成功（16 字节）→ 第 3 个失败且 `GetDataSize()` 仍是 16、前 16 字节内容未被破坏」。

#### 4.2.4 代码实践

**实践目标**：亲手复现越界保护行为（改参数观察型实践）。

**操作步骤**：

1. 阅读 `AppendOutOfBounds`（上文链接），记下它用的容量 `CreateCap(20)` 与断言。
2. 在本地把该用例复制一份（可加到本讲综合实践的新测试文件里），把容量依次改成 `24`、`16`，其余不动。
3. 运行 `bash tests/run_test.sh -u`，用 `--gtest_filter=` 过滤到这条用例。

**需要观察的现象**：容量 24 时第 3 个 `int64_t` 追加成功、`GetDataSize()` 变为 24；容量 16 时第 2 个就失败、`GetDataSize()` 停在 8。

**预期结果**：验证「失败即停、不写半截数据」。具体运行输出**待本地验证**（本环境未执行编译）。

#### 4.2.5 小练习与答案

**练习 1**：数组版 `Append` 为什么错误码区分 `GRAPH_MUL_OVERFLOW` 和 `GRAPH_ADD_OVERFLOW`？

**答案**：两处失败点不同——`sizeof(T) × append_num` 乘法溢出返回 `GRAPH_MUL_OVERFLOW`；乘积本身没溢出但 `data_size_ + append_size` 加法溢出（或超容量）时走 `Expand` 返回 nullptr，返回 `GRAPH_ADD_OVERFLOW`。调用方可据此区分「单次追加量本身非法」和「累计超限」。

**练习 2**：如果 `Expand` 先加 `data_size_` 再检查容量，和现在「先检查再更新」有什么差别？

**答案**：现在的实现顺序是检查全部通过后才更新 `data_size_`，失败路径完全不动状态（幂等、可重试）；若先更新再检查，失败后长度已被改大，后续 Append 会从错误偏移写入，字节流被污染。

**练习 3**：`operator<<` 连续链式追加 `td << a << b << c;` 有什么风险？

**答案**：`operator<<` 内部调用 `Append` 但丢弃返回值（头文件注释明确说明无法抛错），任何一次容量不足都会被静默吞掉，产生不完整的 tiling 数据且无任何报错。应改用逐个检查返回值的 `Append`。

### 4.3 AppendConvertedAttrVal：属性到字节流的类型转换桥

#### 4.3.1 概念说明

很多算子的 tiling 结果里需要包含一部分「从属性直接搬进 tiling data」的常量（例如标尺、缩放系数），但属性在上下文中的存储类型（如 `float`）未必等于设备侧希望的类型（如 `float16` 的 16 位表示）。`AppendConvertedAttrVal` 把这两步合一：按「源类型 → 目的类型」从上下文属性取值、转换、追加进 `TilingData`。

类型空间由 `AttrDataType` 枚举定义，它是 ABI 契约——**只能在尾部追加，不能插入**（和 [u2-l1](u2-l1-datatype-and-format.md) 的 `DataType`/`Format` 枚举同一规则）。

#### 4.3.2 核心流程

```text
AppendConvertedAttrVal(attrs, idx, src_type, dst_type)
  │
  ├─ attrs 为空 / attr_index 越界 ──────────► GRAPH_FAILED（打日志）
  │
  ├─ kAttrTable.Find(src_type, dst_type)
  │     │  二维表 [src][dst] → 函数指针；未登记的组合 → nullptr
  │     ▼
  ├─ func == nullptr ──────────────────────► GRAPH_FAILED（组合不支持）
  │
  └─ func(this, attrs, attr_index)
        │  例如 AppendConvertedAttr<float, int32_t>：
        │    GetAttrPointer<float>(idx) 取属性
        │    IntegerChecker 检查目标类型可容纳（仅告警）
        │    static_cast<int32_t> 后 Append
        ▼
     GRAPH_SUCCESS
```

查找表大小为 \( |\text{kTypeEnd}| \times |\text{kTypeEnd}| \)，即枚举值个数（含结尾哨兵 `kTypeEnd`）的平方，每个格子存一个 `std::function`。

#### 4.3.3 源码精读

- [inc/external/exe_graph/runtime/tiling_data.h:25-65](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L25-L65)：`AttrDataType` 枚举，从 `kBool` 到 `kTypeEnd` 共 38 个值，覆盖标量/list/list-list 与 fp16/bf16 变体。
- [base/runtime/tiling_data.cc:597-613](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L597-L613)：入口 `AppendConvertedAttrVal`。三段式：判空判越界 → 查表 → 执行。注意它声明在头文件（[tiling_data.h:194-195](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L194-L195)）而实现在 `libmetadef` 所在编译单元，因为查找表体积太大不适合放头文件。
- [base/runtime/tiling_data.cc:418-443](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L418-L443)：`AttrTable<SRC, DST>` 二维函数表模板，构造时全部填 `default_val`（nullptr），`Add` 链式登记，`Find` 越界返回 nullptr。
- [base/runtime/tiling_data.cc:445-593](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L445-L593)：`kAttrTable` 常量表登记现场。以 `.Add(kInt32, kInt32, &AppendAttr<int32_t>)`（同型直通）与 `.Add(kInt32, kInt64, &AppendConvertedAttr<int32_t,int64_t>)`（转换）为代表，float→float16/bfloat16 有专门的位转换函数（如 [L478-L483](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L478-L483)）。
- [base/runtime/tiling_data.cc:50-66](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L50-L66)：两个最基础的执行器——`AppendAttr<T>`（取标量属性指针后 `Append(*attr)`）与 `AppendListAttr<T>`（取 `ContinuousVector` 属性，按元素个数数组版 Append）。`GetAttrPointer<T>` 来自 `RuntimeAttrs`（[u3-l1](u3-l1-kernel-context.md) 提过属性槽经 compute_node_info 访问）。
- [base/runtime/tiling_data.cc:86-95](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L86-L95)：`AppendConvertedAttr<T1,T2>` 转换执行器。`IntegerChecker<T2>::Compat` 只做溢出**告警**（`GELOGW`）不拦截——窄化转换照常执行，这是需要留意的取舍。
- [tests/ut/base/testcase/tiling_data_unittest.cc:27-47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc#L27-L47)：`BuildTestContext` 用 `OpTilingContextBuilder`（[u5-l1](u5-l1-context-builder.md) 将详述）注入 9 个不同类型的属性，是全部属性转换单测的公共夹具。
- [tests/ut/base/testcase/tiling_data_unittest.cc:1954-1987](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc#L1954-L1987)：三个非法路径用例——属性下标越界、不支持的源类型、不支持的目标类型，均断言 `GRAPH_FAILED`。

#### 4.3.4 代码实践

**实践目标**：从测试断言反推 `kAttrTable` 的覆盖范围，理解「哪些转换合法」。

**操作步骤**：

1. 在 [base/runtime/tiling_data.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc) 中检索 `.Add(AttrDataType::kString`，数一数 string 作为源类型登记了几个组合。
2. 对照 `AppendAttrSrcTypeInvalid` 用例（[tiling_data_unittest.cc:1964-1974](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc#L1964-L1974)）：`kString → kInt32` 断言失败，但 `AppendAttrStrOk`（[L141-L154](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tiling_data_unittest.cc#L141-L154)）里 `kString → kString` 成功。
3. 思考：若给 `AttrDataType` 中间插入一个新枚举值，`kAttrTable` 和已有测试会发生什么？

**需要观察的现象 / 预期结果**：string 只登记了 `kString→kString` 一个组合（走 `AppendStrAttr`，见 [base/runtime/tiling_data.cc:151-156](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L151-L156)，按 `strlen` 追加、不含结尾 `\0`）；中间插枚举值会使 `kTypeEnd` 之前所有值的整数编码平移，属于破坏 ABI 的改动，函数指针表的下标含义也随之错乱。

#### 4.3.5 小练习与答案

**练习 1**：为什么用二维函数指针表而不是一个巨大的 switch-case？

**答案**：表在编译期由模板 `AttrTable<SRC,DST>` 生成、初始化一次（静态常量 `kAttrTable`），`Find` 只是两次数组下标取值，O(1) 且无需在运行期做字符串或类型比较；新增组合只需 `.Add` 一行，不必改动查找逻辑。switch-case 则要把「src × dst」的叉积全部展开在一个函数里，难以维护。

**练习 2**：`float → bool` 的转换规则是什么？从哪里看出来的？

**答案**：`GetValue<float,bool>` 特化（[base/runtime/tiling_data.cc:25-28](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L25-L28)）：绝对值大于 `float` 的机器精度（epsilon）即为 true，否则 false。不是 C 风格的「非零即真」对极小值的处理——`1e-40f` 会转成 false。

**练习 3**：属性下标越界时函数返回前有没有副作用？

**答案**：没有。越界检查在最前面（[base/runtime/tiling_data.cc:600-603](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/runtime/tiling_data.cc#L600-L603)），直接打日志返回 `GRAPH_FAILED`，不会触碰 `TilingData` 的字节流；与 `Append` 的「失败不写半截」语义一致。

### 4.4 tilingdata_base.h 宏体系：带类型的 TilingDef 与工厂注册

#### 4.4.1 概念说明

`gert::TilingData` 只给了一串无类型字节。算子开发者更希望按**字段名**读写（`set_block_dim(8)`），且字段布局自动对齐。`optiling` 命名空间下的 `tilingdata_base.h` 用「基类 + 宏」实现这一点：

- `TilingDef` 基类持有字段元信息表 `field_info_`、数据缓冲 `data_ptr_` 和总长 `data_size_`；
- `BEGIN_TILING_DATA_DEF(X)` 宏展开出一个派生类，其中每个 `TILING_DATA_FIELD_DEF(type, name)` 声明一个带 `set_/get_` 访问器的字段，并在**成员初始化时**通过 `FieldHandler` 把字段偏移登记进基类；
- `REGISTER_TILING_DATA_CLASS(op_type, class_name)` 通过静态对象的构造函数把「op 类型名 → 构造函数」注册进 `CTilingDataClassFactory` 单例，供框架按算子名反查出 tiling 结构体。

这套机制与 [u4-l3](u4-l3-op-def-factory.md) 将要讲的 `OpDefFactory` 注册是同一模式：**匿名命名空间静态对象 + 工厂单例**。

#### 4.4.2 核心流程

```text
算子仓源码中：
BEGIN_TILING_DATA_DEF(MyTiling)
  TILING_DATA_FIELD_DEF(int32_t, block_dim)   ← 每个字段成员初始化时调用 FieldHandler
  TILING_DATA_FIELD_DEF(int64_t, total_size)       登记 FieldInfo 并累加 data_size_
END_TILING_DATA_DEF
REGISTER_TILING_DATA_CLASS(MyOp, MyTiling)    ← so 加载时静态对象构造 → 工厂登记

运行期（宿主侧 tiling 函数里）：
new MyTiling()
  构造函数：先加 8 字节类名占位对齐 → 登记结构体大小 → InitData()
  InitData()：new uint8_t[data_size_] 并让嵌套 struct 字段共享该缓冲
tiling_func 计算 → set_block_dim(8)（写 data_ptr_ + offset）→ SaveToBuffer(buf, cap) 打平传出

框架侧反序列化：
factory.CreateTilingDataInstance("MyOp")
  → new MyTiling(外部指针)  → SetDataPtr 让字段直接落在外部缓冲上
```

对齐规则：每登记一个字段前，`CheckAlignAndGenPlaceHolder` 检查当前 `data_size_` 是否是该字段类型的整数倍，不是则自动插入 `uint8_t` 占位数组补齐（[base/asc/tilingdata_base_impl.cc:91-99](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc#L91-L99)）。

#### 4.4.3 源码精读

- [inc/external/register/tilingdata_base.h:135-145](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L135-L145)：官方使用示例注释——`MaxPoolTilingData` 定义三个不同宽度字段并注册给 `MaxPool` 算子，这是理解宏用法的最短路径。
- [inc/external/register/tilingdata_base.h:146-189](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L146-L189)：`BEGIN_TILING_DATA_DEF` 展开的三要素——三个重载 `FieldHandler`（标量/数组/嵌套 struct，各自登记 `FieldInfo` 并累加 `data_size_`）、默认构造函数（登记结构体大小 + `InitData()` 分配缓冲）、外部指针构造函数（`SetDataPtr` 复用外部内存，用于反序列化侧）。
- [inc/external/register/tilingdata_base.h:191-204](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L191-L204)：`TILING_DATA_FIELD_DEF` 为字段生成 `set_`（同时写成员变量和 `data_ptr_ + offset_` 处的缓冲）、`get_`（读成员变量）、偏移量成员 `field_name##_offset_`（初始化即调用 `FieldHandler`）以及 16 字节 `reserve_buf`（为基类预留演进空间）。
- [inc/external/register/tilingdata_base.h:241-255](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L241-L255)：`REGISTER_TILING_DATA_CLASS` 宏。匿名命名空间里定义 Helper 类，静态实例 `g_tilingdata_##op_type##...` 在 so 加载时构造，把 `CreateTilingDataInstance`（`make_shared<class_name>`）登记进工厂——这是典型的「静态对象自注册」。
- [base/asc/tilingdata_base_impl.cc:101-119](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc#L101-L119)：`InitData` 按累计的 `data_size_` `new` 一块清零缓冲，并让所有嵌套 struct 字段（`saveBufferPtr` 里记录的指针）通过 `SetDataPtr` 落到 `data_ptr_ + offset` 的位置——父结构与子结构**共享同一块连续内存**，这正是能整体打平的前提。
- [base/asc/tilingdata_base_impl.cc:73-89](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc#L73-L89)：`SaveToBuffer` 把 `data_ptr_` 起的 `data_size_` 字节 memcpy 到外部缓冲；若 `inited_data_ptr` 为真（外部缓冲模式）则直接返回不再拷贝。
- [base/asc/tilingdata_base_impl.cc:59-71](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc#L59-L71)：`SetDataPtr` 释放自建缓冲、切换到外部指针，并递归为嵌套 struct 重新定位——反序列化侧「从数据反构造」的实现。
- [base/asc/tilingdata_base_impl.cc:197-203](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc#L197-L203)：工厂 `RegisterTilingData`/`CreateTilingDataInstance` 的对外转发；查不到 op_type 返回 `nullptr`（[L168-186](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/asc/tilingdata_base_impl.cc#L168-L186)），同样是空值失败语义。

#### 4.4.4 代码实践

**实践目标**：手工展开一次宏，搞清 `TILING_DATA_FIELD_DEF(int32_t, x)` 到底生成了什么。

**操作步骤**：

1. 对照 [tilingdata_base.h:191-204](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L191-L204)，在纸上把 `TILING_DATA_FIELD_DEF(int32_t, block_dim)` 展开成完整 C++ 代码（`set_block_dim`、`get_block_dim`、`block_dim_`、`block_dim__offset_`、`block_dim__reserve_buf_` 五个成员）。
2. 追踪 `block_dim__offset_` 的初始化：成员初始化顺序即声明顺序，所以第一个字段的 offset 是「类名占位 8 字节之后」的值；再算第二个 `int64_t` 字段前会插入几个占位字节。
3. 用 `TilingDef::GetDataSize()`（[tilingdata_base.h:82](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/tilingdata_base.h#L82)）核对你的手算结果。

**需要观察的现象 / 预期结果**：结构 `{类名占位 8 字节, int32_t block_dim, [4 字节占位], int64_t total_size}` 的总大小应为 \(8 + 4 + 4 + 8 = 24\) 字节——`int32_t` 后必须补 4 字节占位才能让 `int64_t` 字段 8 字节对齐。这是纯源码推导，结论可靠；如需机器验证可参考综合实践（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：`BEGIN_TILING_DATA_DEF` 生成的默认构造函数里，为什么第一件事是对类名做 `CheckAlignAndGenPlaceHolder(#class_name "PH", 8)`？

**答案**：给整个结构开头强制插入 8 字节对齐的占位（首字段前 padding 到 8 的倍数），保证整块 tiling 缓冲以 8 字节对齐开始——设备侧 DMA 搬运和后续 64 位字段访问都受益于统一的对齐基准。

**练习 2**：`set_xxx` 同时写了成员变量 `xxx_` 和缓冲 `data_ptr_ + offset`，两处冗余吗？

**答案**：不冗余。成员变量是宿主侧的「快速读回」副本（`get_xxx` 读它，不碰缓冲）；缓冲才是真正被 `SaveToBuffer` 序列化的内容。`get_xxx` 走成员可以容忍缓冲尚未分配（外部指针构造前）的场景。

**练习 3**：`REGISTER_TILING_DATA_CLASS` 的 Helper 放在匿名命名空间里，为什么注册仍然全局可见？

**答案**：匿名命名空间只限制**符号链接可见性**（避免多 so 间的符号冲突），而注册动作发生在静态对象构造时，写入的是 `CTilingDataClassFactory` 这个**单例进程级 map**——效果通过共享的工厂对象传出，而非通过符号表。与 [u4-l5](u4-l5-opp-package.md) 的 so 加载时机相关：so 一旦 dlopen，静态对象即完成登记。

## 5. 综合实践

**任务**：参照 `tiling_data_unittest.cc`，亲手完成一次「序列化 → 反序列化 → 断言一致」的闭环，覆盖本讲全部内容：容器创建（4.1）、Append 与容量（4.2）、带字段结构（4.4）。

**步骤**：

1. **新建测试文件** `tests/ut/base/testcase/my_tiling_data_unittest.cc`（[u1-l2](u1-l2-build-and-test.md) 已确认 `ut_metadef` 用 glob 收集该目录，无需改 CMake）。以下为**示例代码**（非仓库原有代码）：

   ```cpp
   #include "exe_graph/runtime/tiling_data.h"
   #include <gtest/gtest.h>

   namespace gert {
   namespace {
   // 两个 int32 字段的自定义 tiling 结构（示例代码）
   struct MyTilingStruct {
     int32_t block_dim;
     int32_t total_size;
   };
   static_assert(std::is_standard_layout<MyTilingStruct>::value, "must be POD");
   }  // namespace

   class MyTilingDataUT : public testing::Test {};

   TEST_F(MyTilingDataUT, SerializeThenDeserialize) {
     // 1. 创建容量 64 字节的容器（头体分离的一整块内存）
     auto data = TilingData::CreateCap(64);
     auto td = reinterpret_cast<TilingData *>(data.get());
     ASSERT_NE(td, nullptr);

     // 2. 序列化：按字段追加，检查返回值（不要用 operator<<）
     const MyTilingStruct src{.block_dim = 8, .total_size = 1024};
     ASSERT_EQ(td->Append(src), ge::GRAPH_SUCCESS);
     EXPECT_EQ(td->GetDataSize(), sizeof(MyTilingStruct));  // 8 字节

     // 3. 模拟跨边界：只拿「字节流 + 长度」，从零反序列化
     const size_t len = td->GetDataSize();
     std::vector<uint8_t> wire(len);
     memcpy(wire.data(), td->GetData(), len);   // 模拟搬运动作
     const auto *dst = reinterpret_cast<const MyTilingStruct *>(wire.data());

     // 4. 断言字段一致
     EXPECT_EQ(dst->block_dim, 8);
     EXPECT_EQ(dst->total_size, 1024);
   }
   }  // namespace gert
   ```

2. **追加一个越界用例**（覆盖 4.2）：把容量改成 `CreateCap(4)`，断言 `Append` 返回非 `GRAPH_SUCCESS` 且 `GetDataSize()` 保持 0。
3. **编译运行**：`bash tests/run_test.sh -u`，若只想跑自己的用例，在 ctest/测试二进制上加 `--gtest_filter=MyTilingDataUT.*`（具体过滤方式随 run_test.sh 输出而定）。
4. **观察**：两条用例均 PASS；刻意把 `EXPECT_EQ(dst->block_dim, 8)` 改成 9 验证测试真的在检查（红→绿）。

**预期结果**：`GetDataSize()` 为 8；反序列化后两字段与写入值一致；容量不足时追加失败且不污染数据。完整运行输出**待本地验证**（本环境未执行编译）。

## 6. 本讲小结

- `gert::TilingData` 是「64 字节头部 + 尾随数据区」的变长 POD 容器，`CreateCap` 一次性分配头体连续内存，`static_assert(is_standard_layout)` 固化 ABI 契约，禁拷贝防头部与数据区脱钩。
- `Append`/`Expand` 是唯一正规写入路径：全部长度算术带溢出检查，失败时不写半截数据；`operator<<` 会吞错误，工程代码应使用返回 `graphStatus` 的 `Append`。
- `AppendConvertedAttrVal` 通过 `AttrDataType` 枚举 + 二维函数指针表 `kAttrTable` 实现「属性取值 → 类型转换 → 追加」的 O(1) 分发；枚举取值是 ABI 契约，只能尾部追加。
- `tiling_context.h` 的 `GetTilingData<T>()` 是容器与 tiling 阶段的衔接点：容量校验后把结构体覆写进字节流并 `SetDataSize` 登记长度。
- `optiling::TilingDef` 宏体系在无类型字节流之上提供「按字段名读写」：`FieldHandler` 在成员初始化时登记偏移并自动插占位对齐，`SaveToBuffer`/`SetDataPtr` 完成打平与从数据反构造，`REGISTER_TILING_DATA_CLASS` 借静态对象把构造函数登记进工厂单例。

## 7. 下一步学习建议

本讲完成了单元三（exe_graph 运行时上下文）的最后一块拼图。接下来有两个方向：

1. **进入单元四**：从 [u4-l1 register 模块总览](u4-l1-register-overview.md) 开始，理解 tiling 函数（连同 InferShape 等）如何经 `OpImplRegisterV2` 以裸函数指针注册到框架——这会解释「谁在调用传入 TilingContext 的那个函数」。
2. **若想先补上下文构建侧**：阅读 [u5-l1 ContextBuilder 体系](u5-l1-context-builder.md)，看 `OpTilingContextBuilder` 如何构造本讲单测里 `BuildTestContext` 那样的 TilingContext（含 `kOutputTilingData` 槽位里预分配的 `TilingData` 容器）。
