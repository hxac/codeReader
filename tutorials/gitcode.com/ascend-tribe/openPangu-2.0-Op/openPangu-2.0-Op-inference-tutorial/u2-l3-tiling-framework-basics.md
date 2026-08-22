# op_host 之 Tiling：TilingData 与 TilingBaseClass 七步框架

## 1. 本讲目标

上一讲（u2-l2）我们读完了 aclnn 两段式接口，结尾留了一个钩子：aclnn 层只是把算子「登记」进下发列表，真正的执行计划——数据怎么切、用几个核、每次搬多少——是在**登记时连带触发的 Tiling 计算**里完成的。本讲就补上这块拼图。

学完本讲，你应该能够：

1. 使用 `BEGIN_TILING_DATA_DEF` / `TILING_DATA_FIELD_DEF` 宏定义一份 TilingData，并说出每个字段在 kernel 侧的用途。
2. 按顺序说出 `TilingBaseClass::DoTiling()` 七个步骤（`GetShapeAttrsInfo → GetPlatformInfo → IsCapable → DoOpTiling → DoLibApiTiling → GetWorkspaceSize → PostTiling`）各自做什么、本算子在哪一步填了哪些字段。
3. 解释 TilingData 如何从 host 侧的计算结果变成 kernel 侧可直接读取的结构体（`SaveToBuffer` → `GET_TILING_DATA`）。
4. 说清 TilingKey 的完整闭环：host 侧 `GetTilingKey()` → 框架 `SetTilingKey` → kernel 侧 `TILING_KEY_IS` 分支。

## 2. 前置知识

### 2.1 为什么需要 Tiling

回顾 u1-l3 的结论：**Host 只算计划，Device 只执行**。之所以要这样分工，是因为 kernel 在设备上运行时「不知道」三件事：

- 输入张量的运行时形状（编译时只知道 dtype，shape 是动态的）；
- 这块芯片有多少个核、UB（Unified Buffer，向量核心的片上高速缓存）有多大；
- 数据应该按什么粒度在 GM（Global Memory，设备主存）和 UB 之间搬运。

Tiling 就是 host 侧的一段 C++ 函数：在算子被登记执行前，读取形状与平台信息，把上面三个问题的答案算成一组整数参数，打包传给 kernel。这组参数就是 **TilingData**，可以理解为 kernel 的「施工图」。

### 2.2 gert::TilingContext：tiling 函数的「眼睛」

tiling 函数不直接接触张量数据，它通过 CANN 传入的 `gert::TilingContext*` 拿到一切输入信息：输入描述（dtype）、输入形状（含 stride）、平台信息（核数、UB 大小）、以及用来写回结果的 `RawTilingData` 缓冲区。本讲会反复看到 `context_->GetInputDesc(...)`、`context_->GetPlatformInfo()` 这类调用。

### 2.3 PlatformInfo 与 CompileInfo：平台信息的两条来路

同一份 tiling 代码可能在两种场景被调用：

- **编译期/图编译场景**：能拿到完整的 `PlatformInfo`，可直接查询核数与 UB 大小；
- **运行期（二进制已编译好）场景**：拿不到完整平台信息，此时退化为读取编译期缓存好的 **CompileInfo**（一个算子自定义的小结构体，比如本算子的 `aivNum`、`ubSize`）。

本算子的 `GetPlatformInfo()` 两条路都实现了，这是本仓库 tiling 代码的标准写法，后面精读时会看到。

### 2.4 与前两讲的衔接

- u2-l1 讲过 OpDef 注册的是算子「签名」；本讲的 tiling 类注册的是算子的「执行计划生成器」，两者都挂在同一个算子名（如 `AiInfraScatterBlockUpdate`）下。
- u2-l2 讲过 aclnn 层通过 `ADD_TO_LAUNCHER_LIST_AICORE` 登记算子；登记会触发本讲的 tiling 流程，tiling 的产物（TilingData、TilingKey、blockDim、workspace）随执行请求一起下发。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h` | 定义 TilingData 的 15 个字段、CompileInfo 结构，并声明 tiling 类 `AiInfraScatterBlockUpdateBaseTiling` 及其对七步虚函数的覆写 |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp` | 七步框架的具体实现、分核与 UB 切分算法、tiling 类的注册（`REGISTER_TILING_TEMPLATE` + `IMPL_OP_OPTILING`） |
| `ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h` | 公共基类 `TilingBaseClass`：固化了七步执行顺序的 `DoTiling()` 模板方法与三态返回值约定 |
| `ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h` | tiling 模板注册表：按优先级轮询多个 tiling 类的机制 |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp` | kernel 入口：`GET_TILING_DATA` 解包 + `TILING_KEY_IS` 分支，TilingData 的消费端 |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h` | Kernel 类 `Init`：逐字段读取 TilingData 的示例 |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/test_ai_infra_scatter_block_update_tiling.cpp` | tiling 单元测试：无硬件验证 tiling 逻辑的入口 |

## 4. 核心概念与源码讲解

### 4.1 TilingData 定义：从字段声明到二进制流

#### 4.1.1 概念说明

TilingData 是 host 与 device 之间的一份**数据契约**：host 侧把切分参数按字段顺序写进一块连续缓冲区，kernel 侧按同样的字段顺序读出来。它本质是一个「序列化结构体」——不是直接传 C++ 对象（host 是 x86/ARM CPU，device 是昇腾核心，不能共享对象），而是传字节的排列。

CANN 用一组宏让两边共享同一个字段定义，host 侧用 `set_xxx()` 写、kernel 侧用 `xxx` 成员读，字段顺序与对齐由宏统一保证。

#### 4.1.2 核心流程

定义一份 TilingData 只需三步：

```text
1. BEGIN_TILING_DATA_DEF(XxxTilingData)   开始结构定义
2. TILING_DATA_FIELD_DEF(类型, 字段名)     逐个声明字段（顺序即序列化顺序）
   ... END_TILING_DATA_DEF;               结束定义
3. REGISTER_TILING_DATA_CLASS(算子名, XxxTilingData)
   把结构体与算子名绑定，供框架与 kernel 侧反解
```

本算子共声明 **15 个字段**，按用途分四组：

- 总量描述：`totalIndicesCount`（要写入多少行）、`updateDimSize`（每行多少元素，即 D）；
- 平台快照：`totalCoreNum`、`ubSize`；
- 切分策略（kernel 真正的施工参数）：`eachCoreIndexCount`、`usedCoreNum`、`tailCoreIndexCount`、`oneIndexSize`、`oneUpdateAlignSize`、`indicesPerLoad`、`maxIndicesPerLoad`、`inputStride0`、`inputStride1`；
- 类型大小：`indicesTypeSize`、`updateTypeSize`。

#### 4.1.3 源码精读

TilingData 的字段定义（15 个字段按声明顺序序列化）：
[ai_infra_scatter_block_update_tiling.h:25-43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h#L25-L43)
：`BEGIN_TILING_DATA_DEF(AiInfraScatterBlockUpdateTilingData)` 与 `END_TILING_DATA_DEF` 之间用 `TILING_DATA_FIELD_DEF` 逐个声明字段，最后 `REGISTER_TILING_DATA_CLASS(AiInfraScatterBlockUpdate, AiInfraScatterBlockUpdateTilingData)` 把结构体绑定到算子名。

同文件中还有 CompileInfo 结构：
[ai_infra_scatter_block_update_tiling.h:45-48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h#L45-L48)
：`AiInfraScatterBlockUpdateCompileInfo` 只有两个字段 `aivNum`（向量核数）和 `ubSize`，它是编译期由 `TilingParse` 函数填充、运行期 tiling 时读取的平台快照（见 4.2 节）。

host 侧「写」的动作发生在 `PostTiling()`：
[ai_infra_scatter_block_update_tiling.cpp:402-404](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L402-L404)
：先校验 TilingData 总大小按 8 字节对齐（设备侧搬运的硬性要求），然后 `tilingData_.SaveToBuffer(context_->GetRawTilingData()->GetData(), ...GetCapacity())` 把字段序列化进框架提供的缓冲区，`SetDataSize` 告诉框架实际写了多少字节。

kernel 侧「读」的动作在入口函数：
[ai_infra_scatter_block_update.cpp:25-35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp#L25-L35)
：kernel 入口的第 6 个参数 `tiling` 就是 host 侧 `SaveToBuffer` 写入的那块内存，`GET_TILING_DATA(tilingData, tiling)` 把它反序列化为结构体 `tilingData`，随后整个结构体按引用传给 Kernel 类的 `Init`。

Kernel 类逐字段消费 TilingData：
[ai_infra_scatter_block_update.h:73-93](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L73-L93)
：`Init` 从 `tiling.updateDimSize`、`tiling.eachCoreIndexCount` 等字段取值存入 Kernel 类成员，并用 `tiling.maxIndicesPerLoad * tiling.oneIndexSize` 等计算 `TPipe::InitBuffer` 的队列大小——这就是「按施工图备料」。

值得注意：`Init` 显式读取了 15 个字段中的 9 个（`updateDimSize`、`eachCoreIndexCount`、`tailCoreIndexCount`、`usedCoreNum`、`maxIndicesPerLoad`、`inputStride0/1`、`oneIndexSize`、`oneUpdateAlignSize`）；其余如 `totalIndicesCount`、`ubSize`、`indicesTypeSize` 等主要服务于 host 侧计算过程与日志调试（类型大小在 kernel 侧由编译期 `sizeof(T)` 得到，见 kernel 入口文件头注释对 `DTYPE_INPUT` 宏的说明）。

#### 4.1.4 代码实践

1. **实践目标**：建立「TilingData 字段 ↔ kernel 消费」的对应感。
2. **操作步骤**：
   - 打开 tiling.h 的字段定义（上面第一个链接），把 15 个字段抄成一张三列表格：字段名 / 含义 / kernel 是否在 `Init` 中读取；
   - 再打开 kernel 头文件 `Init` 的实现逐一核对第三列。
3. **需要观察的现象**：切分策略组的 8 个字段几乎全部被 kernel 消费，而「总量/平台/类型大小」组大多不被直接消费。
4. **预期结果**：能说出至少一个「host 填了但 kernel 未直接读」的字段（如 `ubSize`）及其存在的理由（记录 host 决策依据，便于日志排查）。
5. 本实践为源码阅读型，无需运行环境，结论可直接从代码得出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 TilingData 必须用宏定义字段，而不能直接传一个普通 C++ 结构体指针给 kernel？

**答案**：host（CPU）与 device（昇腾核心）不共享地址空间与对象布局，只能传按约定排列的字节流；宏统一生成序列化/反序列化代码（host 侧 `set_xxx`/`SaveToBuffer`，kernel 侧 `GET_TILING_DATA`），保证两端字段顺序、对齐一致。此外代码里还要求总大小 8 字节对齐（`PostTiling` 中的校验），这是设备侧数据搬运的对齐要求。

**练习 2**：`AiInfraScatterBlockUpdateCompileInfo` 和 `AiInfraScatterBlockUpdateTilingData` 都是结构体，它们传给 kernel 吗？

**答案**：不。CompileInfo 只存在于 host 侧：编译期由 `TilingParse` 填充、运行期 tiling 时作为平台信息的替代来源（见 4.3.1）；TilingData 才是唯一会序列化传到 device 侧的结构。

### 4.2 TilingBaseClass 七步框架：DoTiling 的执行骨架

#### 4.2.1 概念说明

如果把每个算子的 tiling 逻辑各写各的，代码会千奇百怪、难以审查。本仓库在 `common/include/tiling_base/tiling_base.h` 提供了公共基类 `TilingBaseClass`，用**模板方法模式**把 tiling 固化为七个步骤：基类的 `DoTiling()` 规定步骤与顺序，子类按需覆写各个虚函数。本仓库所有 ops-transformer 算子的 tiling 类都继承它。

七步及其分工（与源码注释一一对应）：

| 步骤 | 虚函数 | 职责 | 本算子的实现 |
| --- | --- | --- | --- |
| 1 | `GetShapeAttrsInfo()` | 读取输入/输出/属性信息 | 空实现，直接返回成功（形状检查挪到 DoOpTiling 里做） |
| 2 | `GetPlatformInfo()` | 获取核数、UB/L1/L0C 等平台资源 | 读 PlatformInfo 或 CompileInfo，填 `aivNum_`、`ubSize_` |
| 3 | `IsCapable()` | 判断本 tiling 类是否支持当前场景 | 恒返回 true（本算子只注册了一个 tiling 类） |
| 4 | `DoOpTiling()` | 计算数据切分，是 tiling 的核心 | 形状检查 + 分核 + UB 容量计算 |
| 5 | `DoLibApiTiling()` | 计算高阶 API（如 matmul 类库接口）的 tiling | 空实现（本算子不用高阶 API） |
| 6 | `GetWorkspaceSize()` | 计算 workspace 大小 | 空实现（`workspaceSize_` 用基类默认值 0，本算子原地更新无需 workspace） |
| 7 | `PostTiling()` | 保存 TilingData | 把所有中间变量写入 `tilingData_` 并 `SaveToBuffer`、设置 blockDim 与 workspace |

框架在 `PostTiling` 成功后还会统一执行 `context_->SetTilingKey(GetTilingKey())`——注意这一步在基类里，不在七步虚函数内（见 4.4 节）。

#### 4.2.2 核心流程

`DoTiling()` 的执行流程（任何一步失败即整体失败返回）：

```text
DoTiling()
 ├─ 1 GetShapeAttrsInfo()   ─失败→ 返回失败
 ├─ 2 GetPlatformInfo()     ─失败→ 返回失败
 ├─ 3 IsCapable()?          ─false→ 返回 GRAPH_PARAM_INVALID（本类不支持）
 ├─ 4 DoOpTiling()          ─失败→ 返回失败
 ├─ 5 DoLibApiTiling()      ─失败→ 返回失败
 ├─ 6 GetWorkspaceSize()    ─失败→ 返回失败
 ├─ 7 PostTiling()          ─失败→ 返回失败
 └─ context_->SetTilingKey(GetTilingKey())   ← 框架统一收尾
     返回 GRAPH_SUCCESS
```

**三态返回值**是这套框架的精髓（源码注释原文见下方链接）：

- `GRAPH_SUCCESS`：成功，且**不需要**继续执行后续 tiling 类的实现；
- `GRAPH_FAILED`：失败，中止整个 tiling 流程；
- `GRAPH_PARAM_INVALID`：本类不支持，**需要继续往下轮询其他 tiling 类**。

配合注册表按 priority 从小到大遍历，就实现了「一个算子注册多个 tiling 类，先特化后通用」的择优机制——本算子只注册了一个类，体会不到轮询，但第 5 单元（u5-l1）讲的 kv_rms_norm_rope_cache「base → arch35」多模板链正是靠它。

#### 4.2.3 源码精读

基类固化七步顺序的模板方法：
[tiling_base.h:70-102](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L70-L102)
：`DoTiling()` 依次调用七个虚函数，任何一步返回值不是 `GRAPH_SUCCESS` 就直接透传返回；全部成功后由**基类**调用 `context_->SetTilingKey(GetTilingKey())` 完成收尾——这就是「子类只管算，框架管落账」。

三态返回值约定与七个纯虚函数声明：
[tiling_base.h:66-125](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L66-L125)
：注释明确了 `GRAPH_PARAM_INVALID` 表示「本类不支持，需要继续往下执行其他 Tiling 类的实现」；七个 `= 0` 纯虚函数即七步，子类必须全部给出（哪怕空实现）。

子类对七步的覆写声明（本算子只实打实做了 4 步）：
[ai_infra_scatter_block_update_tiling.h:57-84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h#L57-L84)
：`GetShapeAttrsInfo`、`DoLibApiTiling`、`GetWorkspaceSize` 都是内联空实现（直接 `return ge::GRAPH_SUCCESS`），`IsCapable` 恒 true，真正有逻辑的是 `GetPlatformInfo`、`DoOpTiling`、`PostTiling` 与 `GetTilingKey`——最小算子也印证了七步中「第 2、4、7 步」是必做项。

基类还提供了受保护的公共状态：
[tiling_base.h:198-204](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L198-L204)
：`context_`（tiling 上下文）、`workspaceSize_`、`tilingKey_` 等成员由子类直接使用——本算子 `DoOpTiling` 里写 `tilingKey_ = FULL_LOAD_TILING_KEY`、`PostTiling` 里写 `workspaces[0] = workspaceSize_` 用的正是基类成员。

那么 `DoTiling()` 是被谁调用的？先看算子级入口注册：
[ai_infra_scatter_block_update_tiling.cpp:418-454](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L418-L454)
：`TilingFunc4ScatterBlockUpdate` 只有一行有效代码——转调 `TilingRegistry::GetInstance().DoTilingImpl(context)`；`TilingPrepare4ScatterBlockUpdate` 在编译期把核数与 UB 大小填进 CompileInfo；文件末尾的 `IMPL_OP_OPTILING(AiInfraScatterBlockUpdate).Tiling(...).TilingParse<...>(...)` 把这两个函数挂到算子上。这是 CANN 的算子 tiling 注册机制，u2-l1 的 `OP_ADD` 注册原型、本讲的 `IMPL_OP_OPTILING` 注册 tiling，两者共同构成一个算子的 host 侧登记。

tiling 类注册进优先级表：
[ai_infra_scatter_block_update_tiling.cpp:78](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L78)
：`REGISTER_TILING_TEMPLATE("AiInfraScatterBlockUpdate", AiInfraScatterBlockUpdateBaseTiling, 1000)`——一个静态对象在 main 之前把「算子名 → tiling 类工厂 → 优先级 1000」写进全局注册表（宏展开见 [tiling_templates_registry.h:330-332](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L330-L332)，注册表用 `std::map<int32_t, TilingClassCase>` 存储故天然按优先级排序，见 [tiling_templates_registry.h:43-51](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L43-L51)）。

注册表按优先级轮询：
[tiling_templates_registry.h:245-262](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L245-L262)
：`DoTilingImpl` 从 map 头部（最小优先级）开始，逐个构造 tiling 类并调用其 `DoTiling()`；只要返回值不是 `GRAPH_PARAM_INVALID`（即成功或硬失败）就终止轮询返回，全部返回 `GRAPH_PARAM_INVALID` 才报「no valid template is found」。

#### 4.2.4 代码实践

1. **实践目标**：亲手走通「算子 tiling 被谁调用」的完整链路。
2. **操作步骤**：
   - 从 `IMPL_OP_OPTILING`（tiling.cpp 末尾）出发，沿 `TilingFunc4ScatterBlockUpdate → TilingRegistry::DoTilingImpl → REGISTER_TILING_TEMPLATE 注册的类 → DoTiling()` 画一张调用链图；
   - 在图中每个节点标注：它定义在哪个文件、负责什么、返回什么。
3. **需要观察的现象**：链路上「注册」与「执行」是分离的——`REGISTER_TILING_TEMPLATE` 在程序启动阶段生效（静态初始化），`DoTiling` 在每次算子执行登记时生效。
4. **预期结果**：能不看资料默写出这条 5 节点链路，并解释为什么 `DoTilingImpl` 用 map 而不是 vector（答案：需要按 priority 排序轮询）。
5. 本实践为源码阅读型，无需硬件。

#### 4.2.5 小练习与答案

**练习 1**：一个算子注册了优先级 100 和 200 两个 tiling 类，100 号的 `DoOpTiling` 返回 `GRAPH_FAILED`，会发生什么？

**答案**：`DoTiling()` 把 `GRAPH_FAILED` 原样返回，`DoTilingImpl` 看到「非 `GRAPH_PARAM_INVALID`」立即终止轮询并返回失败——**不会**尝试 200 号类。三态语义是：只有明确返回 `GRAPH_PARAM_INVALID`（本类不支持）才会轮询下一个；硬失败表示「我支持这个场景但算错了」，应当中止并报错。顺带一提，本算子的 `IsCapable` 恒 true 且各步只返回成功/失败，所以从不触发轮询。

**练习 2**：为什么 `SetTilingKey` 写在基类 `DoTiling()` 里，而不是让子类在 `PostTiling` 里自己调？

**答案**：统一收口避免遗漏。TilingKey 是 host 与 kernel 的「分支暗号」（见 4.4），漏设会导致 kernel 选错分支。把 `context_->SetTilingKey(GetTilingKey())` 固化在模板方法最后一步，子类只需专注计算 `tilingKey_`，契约落账由框架保证。

### 4.3 ScatterBlockUpdate 七步落地：每一步填了哪些字段

#### 4.3.1 概念说明

本模块解决本讲规格指定的核心问题：**`GetPlatformInfo` / `DoOpTiling` / `PostTiling` 各自填充了 TilingData 的哪些字段？**

关键区分：tiling 类的**成员变量**（如 `eachCoreIndexCount_`，带下划线）是计算过程中的草稿；`tilingData_`（`AiInfraScatterBlockUpdateTilingData` 类型的成员）才是最终要序列化传给 kernel 的成品。前六步都在草稿上算，只有第七步 `PostTiling` 把草稿誊写成成品。

#### 4.3.2 核心流程

本算子 tiling 的核心算法（`DoOpTiling`）分四步，设索引总行数为 \( T \)、总核数为 \( N \)、单行 indices 字节数 \( s_i \)、单行 update 对齐后字节数 \( s_u \)：

**分核策略**（前 \( N-1 \) 个核满载，最后一个核吃尾块）：

\[ \text{eachCoreIndexCount} = \left\lceil \frac{T}{N} \right\rceil \]

\[ \text{usedCoreNum} = \left\lceil \frac{T}{\text{eachCoreIndexCount}} \right\rceil \leq N \]

\[ \text{tailCoreIndexCount} = T - \text{eachCoreIndexCount} \times (\text{usedCoreNum} - 1) \]

**UB 容量约束**（double buffer 把可用 UB 对半分，预留 4KB 栈空间）：

\[ \text{indicesPerLoad} = \frac{\lfloor (UB - 4096) / 2 \rfloor}{s_i + s_u} \]

**每次搬运上限**：

\[ \text{maxIndicesPerLoad} = \min(\text{eachCoreIndexCount},\ \text{indicesPerLoad},\ 4064) \]

直觉：先把 T 行尽量平均「铲」到各核（尾核可能少干点活），再算一颗核一次能从 GM 端多少行进 UB（受 UB 一半容量与 4064 上限约束），kernel 据此循环搬运。

#### 4.3.3 源码精读

`GetPlatformInfo`——平台信息两条来路：
[ai_infra_scatter_block_update_tiling.cpp:81-101](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L81-L101)
：优先用 `context_->GetPlatformInfo()`（配合 `PlatformAscendC` 查 `GetCoreNumAiv` 与 UB 大小）；拿不到时退回 `GetCompileInfo()` 反解成 `AiInfraScatterBlockUpdateCompileInfo` 读取 `aivNum`/`ubSize`。结果只写进成员变量 `aivNum_`、`ubSize_`（**此步不碰 `tilingData_`**）。

`DoOpTiling` 前半——形状检查与总量：
[ai_infra_scatter_block_update_tiling.cpp:279-299](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L279-L299)
：先经 `GetAndCheckInputShape()` 完成五连检（input 形状/stride、indices 形状、update 形状、取值范围、dtype 字节数，各自是独立的 protected 成员函数），随后填 `totalIndicesCount_ = T_`、`totalCoreNum_ = aivNum_`，并对空 tensor（T=0）提前置零返回——注意空 tensor 也走 `PostTiling`，只是所有切分字段为 0。

`DoOpTiling` 核心四步：
[ai_infra_scatter_block_update_tiling.cpp:301-350](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L301-L350)
：步骤 1 算单行对齐大小（`oneIndexSize_ = 2 × indicesTypeSize`，`oneUpdateAlignSize_ = AlignUp(D × updateTypeSize, 32)`）；步骤 2 三行代码完成分核（对应上面三个公式）；步骤 3 算 double buffer 下的 `indicesPerLoad_`；步骤 4 两次 `std::min` 得到 `maxIndicesPerLoad_`；最后 `tilingKey_ = FULL_LOAD_TILING_KEY`（值 1000，定义在 [第 59 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L59)）。（注意：此链接中 host 侧与 kernel 侧各有一份同名宏，见 4.4。）

`PostTiling`——誊写与落账：
[ai_infra_scatter_block_update_tiling.cpp:376-416](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L376-L416)
：`set_xxx` 十五连把所有成员变量一次性写入 `tilingData_`（[379-393 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L379-L393)），打印调试日志后做三件落账的事：① 8 字节对齐校验 + `SaveToBuffer` 序列化（[397-404 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L397-L404)）；② `context_->SetBlockDim(usedCoreNum_)` 告诉运行时起多少个核（注意是**实际使用**的核数而非总核数）；③ `workspaces[0] = workspaceSize_`（本算子为 0）。

汇总成本讲核心表格——**七步 × 填充内容对照表**：

| 七步 | 本算子实现 | 填充的成员变量（草稿） | 最终进入 TilingData 的字段 |
| --- | --- | --- | --- |
| 1 GetShapeAttrsInfo | 空 | — | — |
| 2 GetPlatformInfo | 读平台/CompileInfo | `aivNum_`、`ubSize_` | `totalCoreNum`、`ubSize`（经第 7 步誊写） |
| 3 IsCapable | 恒 true | — | — |
| 4 DoOpTiling | 检查 + 切分计算 | `totalIndicesCount_`、`totalCoreNum_`、`eachCoreIndexCount_`、`usedCoreNum_`、`tailCoreIndexCount_`、`oneIndexSize_`、`oneUpdateAlignSize_`、`indicesPerLoad_`、`maxIndicesPerLoad_`、`tilingKey_` | 全部 15 个字段的取值来源 + TilingKey |
| 5 DoLibApiTiling | 空 | — | — |
| 6 GetWorkspaceSize | 空 | `workspaceSize_`（基类默认 0） | （写入 workspaces[0]，非 TilingData 字段） |
| 7 PostTiling | 誊写 + 落账 | `tilingData_`（成品） | 15 个字段全部 `set_xxx` 并 `SaveToBuffer` |

另注：形状与 stride 草稿（`bn_`、`bs_`、`D_`、`inputStride0_`、`inputStride1_`、`T_`、`updateDimSize_` 等）由 `DoOpTiling` 第一步调用的 `GetAndCheckInputShape()` 填充（实现在 [103-276 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L103-L276)），其中非连续 input 的 stride 检测（第 0 维 stride 可能 ≠ `bs_ × D_`）承接了 u2-l2 讲过的 CreateView 原地语义。

UT 如何无硬件验证这一切：
[test_ai_infra_scatter_block_update_tiling.cpp:59-89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/test_ai_infra_scatter_block_update_tiling.cpp#L59-L89)
：用 `gert::TilingContextPara` 直接描述输入输出（shape/dtype/format）与 CompileInfo，交给 `ExecuteTestCase` 走完整的 `TilingFunc4 → DoTilingImpl → DoTiling` 链路，断言期望的返回码、TilingKey（1000）与 workspace。u6-l1 会专门解剖这套 faker 框架，这里只需知道：**tiling 逻辑可以在纯 CPU 上单测验证**。

#### 4.3.4 代码实践

1. **实践目标**：亲手完成上面表格的推导过程（而不是直接看结论）。
2. **操作步骤**：
   - 打开 `ai_infra_scatter_block_update_tiling.cpp`，只读 `GetPlatformInfo`（81-101 行）、`DoOpTiling`（279-353 行）、`PostTiling`（376-416 行）三个函数；
   - 准备两支笔：遇到「写成员变量（带下划线）」记到草稿列，遇到「`tilingData_.set_xxx`」记到成品列；
   - 用网络典型值代入公式验算一遍：\( T = 16384 \)，\( N = 50 \)（AIV 核数），\( D = 128 \)，FP32（`updateTypeSize = 4`），INT32 索引（`indicesTypeSize = 4`），\( UB = 196608 \) 字节（192KB）。
3. **需要观察的现象**：`GetPlatformInfo` 全程不碰 `tilingData_`；`DoOpTiling` 也只写成员变量；唯一出现 `set_xxx` 的地方是 `PostTiling`。
4. **预期结果**：验算值 `eachCoreIndexCount = ⌈16384/50⌉ = 328`，`usedCoreNum = ⌈16384/328⌉ = 50`，`tailCoreIndexCount = 16384 − 328×49 = 192`；`oneUpdateAlignSize = 512`，`oneIndexSize = 8`，`indicesPerLoad = ⌊(196608−4096)/2⌋ / 520 = 96256/520 = 185`（整除向下取），`maxIndicesPerLoad = min(328, 185, 4064) = 185`。（核数以实际平台为准，此处 50 为假设值，代入时请用 `GetCoreNumAiv` 的真实返回。）
5. 本实践为纸面推导 + 源码阅读型；如本机有编译环境，可进一步用 UT 框架把上述期望值写成断言（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `PostTiling` 里的 `context_->SetBlockDim(usedCoreNum_)` 误写成 `SetBlockDim(totalCoreNum_)`，会发生什么？

**答案**：当 `T` 较小、`usedCoreNum_ < totalCoreNum_` 时（例如 T=100、50 核时 usedCoreNum=1），会多起无用核。kernel 侧 `Process` 有防护：`blockIdx >= usedCoreNum_` 的核直接 return（[ai_infra_scatter_block_update.h:99-102](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L99-L102)），所以结果仍正确，但浪费了核资源调度。这体现了 TilingData 自带「运行时自描述」的防御性。

**练习 2**：空 tensor（T=0）分支里 `usedCoreNum_` 置 1 而不是 0，为什么？

**答案**：`SetBlockDim(0)` 是非法配置，运行时至少要起 1 个核执行 kernel 入口；入口里该核读到全零的切分字段后经 `coreCount <= 0` 判断直接返回，形成「起 1 核、空转即退」的安全路径（见 [ai_infra_scatter_block_update_tiling.cpp:289-299](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L289-L299) 与 kernel 的 `coreCount <= 0` 提前返回）。

### 4.4 SetTilingKey：host 与 kernel 的分支暗号

#### 4.4.1 概念说明

TilingKey 是一个 `uint64_t` 整数，随执行请求一起下发，kernel 入口用它选择执行分支。为什么需要它？因为**同一个算子可能有多套 kernel 实现**（比如按数据类型、按是否量化、按核切分方式区分），编译系统会为每个 TilingKey 编出一份二进制；host 侧 tiling 算出本次输入适配哪一套，把编号告诉运行时，kernel 入口再用 `TILING_KEY_IS(n)` 对号入座。

它和 TilingData 的分工：TilingData 传「参数」，TilingKey 选「版本」。u2-l4 将在 kernel 侧展开 `TILING_KEY_IS`，本讲先把 host 侧的闭环讲清。

#### 4.4.2 核心流程

```text
host 侧：
  DoOpTiling 末尾:  tilingKey_ = FULL_LOAD_TILING_KEY (1000)   ← 子类只算不落
  DoTiling 收尾:    context_->SetTilingKey(GetTilingKey())     ← 框架统一落账
kernel 侧：
  入口:             TILING_KEY_IS(FULL_LOAD_TILING_KEY) → 实例化对应 Kernel 类
```

#### 4.4.3 源码精读

host 侧赋值点：
[ai_infra_scatter_block_update_tiling.cpp:350](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L350)
：`DoOpTiling` 计算完成后写基类成员 `tilingKey_`；常量 `FULL_LOAD_TILING_KEY = 1000` 定义在同文件 [第 59 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L59)。

host 侧读取点：
[ai_infra_scatter_block_update_tiling.cpp:371-374](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L371-L374)
：`GetTilingKey()` 只返回 `tilingKey_`；真正的 `SetTilingKey` 由基类 `DoTiling()` 最后一行调用（见 4.2.3 第一条链接）。UT 也断言了它：正常用例 `expectTilingKey = 1000`（[test 文件 84-88 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/test_ai_infra_scatter_block_update_tiling.cpp#L84-L88)）。

kernel 侧对号入座：
[ai_infra_scatter_block_update.cpp:23-35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp#L23-L35)
：kernel 文件自己也定义了同名宏 `#define FULL_LOAD_TILING_KEY 1000`，入口中 `TILING_KEY_IS(FULL_LOAD_TILING_KEY)` 成立才实例化并运行 `ScatterBlockUpdateKernel`。**host 与 kernel 各持一份常量定义，靠数值一致维持契约**——本算子只有一个分支，写法最简；多分支算子（如第 4 单元的 MoE，几十个 TilingKey）会在此基础上扩展成 if-else 链或用位编码。

#### 4.4.4 代码实践

1. **实践目标**：体会 TilingKey 契约的「两处定义、一个数值」的脆弱性。
2. **操作步骤**：做一个思维实验——把 tiling.cpp 第 59 行的 `FULL_LOAD_TILING_KEY` 改成 `2000`（仅改 host 侧），kernel 侧保持 1000，推理会发生什么；再考虑：若只有 kernel 侧改成 2000 呢？
3. **需要观察的现象**（思维推演，不改代码）：两种单向修改都会让 host 下发的 key 与 kernel 期待的 key 不一致。
4. **预期结果**：`TILING_KEY_IS(1000)` 不命中，kernel 入口直接空跑完退出（没有 else 分支），算子「成功执行但输出等于没写」——这是比报错更隐蔽的故障。结论：改 TilingKey 必须两侧同步，或把公共值放到共享头文件。真实运行行为待本地验证。
5. 本实践为代码审查型，建议在纸上完成，不要真的改动源码。

#### 4.4.5 小练习与答案

**练习 1**：TilingKey 和 `REGISTER_TILING_TEMPLATE` 的 priority（本算子是 1000）数值相同，它们是一回事吗？

**答案**：不是，纯属巧合。priority 决定**host 侧多个 tiling 类的轮询顺序**（越小越优先，只在注册表里用）；TilingKey 决定 **device 侧选哪份 kernel 二进制/分支**（随执行请求下发）。两者作用在不同侧、不同阶段。

**练习 2**：为什么 `GetTilingKey()` 被声明为 `const` 成员函数（tiling.h 第 77 行 `uint64_t GetTilingKey() const override`）？

**答案**：它只是读取 `DoOpTiling` 已写好的 `tilingKey_`，不修改任何状态；`const` 限定表达了「查询而非计算」的语义，也让它在 `DoTiling` 收尾处可安全地在常量语境调用。

## 5. 综合实践

**任务：把「尾核吃尾块」切分改成「完全均匀切分」，并找出所有必须同步修改的位置。**

背景：当前分核策略是前 \( N-1 \) 个核各处理 \( \lceil T/N \rceil \) 行、最后一个核处理剩余尾块，理想负载差最多 1 行以内但**最后 一个核可能明显少干**；所谓均匀切分，即让所有被使用的核分到 \( \lfloor T/N \rfloor \) 或 \( \lceil T/N \rceil \) 行（前 \( r = T \bmod N \) 个核多一行）。

**操作步骤**：

1. **改 host 侧分核计算**：替换 [ai_infra_scatter_block_update_tiling.cpp:307-313](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L307-L313) 的三行：
   - `usedCoreNum_ = min(totalCoreNum_, T)`；
   - 引入 `base = T / usedCoreNum_`、`extra = T % usedCoreNum_`（可复用 `oneIndexSize_` 之外的成员或新增草稿变量）；
   - 语义重定义：`eachCoreIndexCount_ = base + 1`（前 extra 个核）、`tailCoreIndexCount_ = base`（其余核）。注意原字段语义「每核行数/尾核行数」已不够表达三态，这正是要观察的设计张力。
2. **同步改 kernel 侧定位公式**：[ai_infra_scatter_block_update.h:104-105](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L104-L105) 的 `coreStart = blockIdx * eachCoreIndexCount_` 与「只有最后一个核是尾核」的假设必须重写为：前 `extra` 个核起点为 `blockIdx * (base+1)`，其余为 `extra*(base+1) + (blockIdx-extra)*base`。**只改 host 不改 kernel 是本任务最常见的错误**——`coreStart` 会整体错位，写入错误位置。
3. **补 UT 断言**：在 `test_ai_infra_scatter_block_update_tiling.cpp` 中选一个 `T` 不能整除核数的用例，把期望的 `eachCoreIndexCount/tailCoreIndexCount`（均匀语义下的新含义）填进 `expectTilingDataStr` 或新增校验。
4. **验证方式**：有环境时用 `bash build.sh -u`（u1-l2 讲过 `-u` 触发 UT 构建，具体子参数以 `bash build.sh -h` 输出为准）跑 tiling 单测；再跑 ST 真机对拍。无硬件时，以 UT 与代码走查为准（待本地验证）。

**预期结果**：能完整列出「host 三行公式、kernel 两行定位、UT 期望值」三类修改点，并理解一条通用法则——**TilingData 的每个字段都是 host 与 kernel 的共享契约，改字段的语义必须两端同步改**。

## 6. 本讲小结

- TilingData 是 host 与 device 间的序列化数据契约：host 用 `BEGIN_TILING_DATA_DEF/TILING_DATA_FIELD_DEF` 定义字段、`PostTiling` 里 `SaveToBuffer` 写入；kernel 入口用 `GET_TILING_DATA` 解包、`Init` 逐字段读取。
- 本仓库所有 tiling 类继承公共基类 `TilingBaseClass`，其 `DoTiling()` 固化七步顺序：`GetShapeAttrsInfo → GetPlatformInfo → IsCapable → DoOpTiling → DoLibApiTiling → GetWorkspaceSize → PostTiling`，最后由基类统一 `SetTilingKey`。
- 七步中真正承重的是第 2、4、7 步：`GetPlatformInfo` 填平台草稿（`aivNum_`/`ubSize_`，双来路：PlatformInfo 或 CompileInfo），`DoOpTiling` 算切分（分核三公式 + UB 容量约束），`PostTiling` 誊写全部 15 个字段并落账（SaveToBuffer、SetBlockDim、workspace）。
- 返回值三态（`GRAPH_SUCCESS/GRAPH_FAILED/GRAPH_PARAM_INVALID`）支撑注册表按 priority 轮询多个 tiling 类，为第 5 单元的多模板算子打底。
- TilingKey 是「分支暗号」：host 侧 `DoOpTiling` 算值、框架 `SetTilingKey` 落账、kernel 侧 `TILING_KEY_IS` 对号入座；本算子两侧各定义一份常量 1000，靠数值一致维持契约。
- tiling 逻辑可以用 UT 框架（`TilingContextPara` + `ExecuteTestCase`）在纯 CPU 上验证，无需昇腾硬件。

## 7. 下一步学习建议

下一讲（u2-l4）跨到界河对岸：**op_kernel 层的 AscendC Kernel 入门**，重点读 kernel 入口宏的参数布局（本讲已见过的 `GM_ADDR` 与 `tiling` 参数从哪来）、`GET_TILING_DATA` 的展开、`TILING_KEY_IS` 分支，以及 Kernel 类 `Init/Process` 两段式与 `TPipe` 内存管理——届时本讲的 `maxIndicesPerLoad`、`tailCoreIndexCount` 等「施工参数」会在 `CopyIn/ScatterOut` 流水线中被逐个消费。

想提前延伸的读者可以：
- 对照阅读 [tiling_templates_registry.h:324-347](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L324-L347)，了解带 SOC 版本的注册宏变体（`REGISTER_TILING_TEMPLATE_WITH_SOCVERSION` 等），为 u5-l1 的多模板轮询做铺垫；
- 浏览一个多 TilingKey 算子的 tiling 头文件（如 `moe/ai_infra_moe_init_routing_v3/op_host/`），直观感受 key 位编码在真实大算子中的样子。
