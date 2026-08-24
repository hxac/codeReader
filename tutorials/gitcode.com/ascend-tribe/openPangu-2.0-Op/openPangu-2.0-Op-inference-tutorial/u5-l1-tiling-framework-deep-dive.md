# u5-l1 公共 Tiling 框架深入：模板注册与 key 体系

## 1. 本讲目标

第 2 单元（u2-l3）我们已经把 Tiling 当成「一个算子、一个 tiling 类、七步流程」来学。本讲深入一层，回答三个问题：

1. **一个算子注册了多个 tiling 类时，到底选哪个？** —— 掌握 `DoTiling` 返回值三态语义，理解它如何驱动注册表按优先级轮询模板（责任链模式）。
2. **TilingKey 的数字是怎么编出来的？** —— 掌握 `tiling_key.h` 的十进制拼装约定，以及仓库中真实算子的手工分段编码套路，理解 host 侧与 kernel 侧的「数字镜像」契约。
3. **ops-nn 侧为什么还需要 tiling 缓存？** —— 理解 `TilingCache` + `MurmurHash` 这套预留基建的设计（哈希索引 + 防冲突比对 + 读写锁），并分清它与 matmul 实际在用的 `op_cache_tiling` 调优缓存路径的边界。

学完本讲，你应该能独立读懂任何一个算子的 tiling 目录，说出它注册了几个模板、优先级次序是什么、每个模板在什么条件下「交棒」。

## 2. 前置知识

本讲默认你已完成 u2-l3（Tiling 七步框架）与 u2-l4（kernel 侧 `TILING_KEY_IS`）。快速回顾并补充几个新概念：

- **Tiling 两级契约**：Host 侧 tiling 类把切分参数序列化进 TilingData（施工图），并通过 `SetTilingKey` 落账一个分支编号；Device 侧 kernel 用 `GET_TILING_DATA` 解包、用 `TILING_KEY_IS(key)` 对号入座。
- **责任链模式（Chain of Responsibility）**：把多个处理者排成一条链，请求沿链传递，每个处理者要么处理、要么说「我不管」交给下一个。本讲的「多个 tiling 模板按优先级轮询」就是它的 C++ 实现。
- **三态返回值**：一般函数只有成功/失败两态；这里多了一态「本模板不支持当前场景」（`GRAPH_PARAM_INVALID`），专门用于告诉框架「跳过我，试下一个」。
- **`std::map` 的有序性**：`std::map<int32_t, T>` 按键升序遍历——这是「priority 越小越先被尝试」的实现基础。
- **静态注册**：C++ 全局对象的构造函数在 `.so` 被加载时执行。注册宏展开成一个 static 全局对象，构造时把类工厂填进全局注册表，从而实现「无需中心化清单文件」的自动注册。
- **MurmurHash**：一种非加密哈希算法，速度快、分布均匀，常用作哈希表的键压缩函数。哈希冲突（两个不同输入得到同一 32 位值）不可避免，所以查表命中后还要做一次全量比对兜底。
- **读写锁（`std::shared_mutex`）**：允许多个读者并发进入，写者独占。tiling 缓存「读多写少」，正适合。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h` | `TilingBaseClass` 七步框架与三态语义的定义处 |
| `ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h` | 模板注册表：`TilingCases`/`TilingRegistry`/注册宏，轮询发生地 |
| `ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h` | TilingKey 的十进制拼装工具（`GET_TILINGKEY`） |
| `ascendc/src/ops-transformer/common/src/tiling_base/tiling_util.cpp` | tiling 公共小工具（regbase 判定占位、标量形状兜底） |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp` | 单模板算子样例（只用一个 tiling 类） |
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/`（tiling.h + base/ds 两个 tiling.cpp + arch35/ 两个） | 多模板算子样例：一条 3 节点模板链 |
| `ascendc/src/ops-nn/matmul/common/op_host/op_tiling/hash.cpp` | MurmurHash 实现（ops-nn 预留缓存基建） |
| `ascendc/src/ops-nn/matmul/common/op_host/op_tiling/tiling_cache.h` | `TilingCache` 线程安全缓存模板 |
| `ascendc/src/ops-nn/matmul/ai_infra_matmul/op_host/ai_infra_matmul_tiling.cpp` | ops-nn 平行注册表的用法 + 实际在用的调优缓存入口 |
| `ascendc/src/ops-nn/common/src/op_host/op_cache_tiling.cpp` | matmul 实际缓存路径：委托 legacy 公共库 |

另外提防一个容易混淆的点：attention 族（FIA/SFA）**没有**用这套公共注册表，而是自建了 `attention/common/op_host/fia_tiling_templates_registry.h`（见 u4-l1）。本讲聚焦公共框架。

## 4. 核心概念与源码讲解

### 4.1 模板轮询：返回值三态与注册表

#### 4.1.1 概念说明

u2-l3 讲过：一个算子的 tiling 入口函数并不直接计算，而是转发给注册表。为什么？因为**同一个算子在不同硬件、不同输入规格下可能需要完全不同的切分策略**。比如 `kv_rms_norm_rope_cache`：

- 在 910B/910_93 上走「DS 模板」（D 维切分）；
- 在 Ascend 950（内部代号 regbase/arch35）上，向量寄存器变宽，优先尝试「D 全载模板」（整行装进 UB）；
- D 实在太大装不下时，再退到「二分重算模板」（fold 折叠累加）。

每种策略一个 tiling 类，注册表按优先级把它们排成一条链。框架逐个实例化、逐个询问，直到有一个类说「我来」。支撑这一切的是 `DoTiling()` 的**三态返回值约定**：

| 返回值 | 含义 | 框架行为 |
| --- | --- | --- |
| `GRAPH_SUCCESS` | 本类完成 tiling | 立即返回成功，**不再**尝试后续模板 |
| `GRAPH_FAILED` | 发生不可恢复错误 | 立即返回失败，**中止整个流程**（不会换下一个模板重试！） |
| `GRAPH_PARAM_INVALID` | 本类不支持当前场景 | 交棒，继续尝试下一个优先级 |

第三态是精髓：模板要么把「不适用」表达成 `GRAPH_PARAM_INVALID`，要么整个算子就直接失败。**把「不支持」误写成 `GRAPH_FAILED` 是接入新模板时最典型的 bug**——它会挡住后面所有模板。

#### 4.1.2 核心流程

一次 tiling 调用的完整时序：

```text
aclnn 下发
  └─ CANN 框架查 OpImpl 注册表，找到该算子的 Tiling 入口函数
       └─ 入口函数（每个算子各写一份）转发 TilingRegistry::GetInstance().DoTilingImpl(context)
            └─ 按 op_type 取出该算子的模板表（std::map<priority, 工厂函数>，升序）
                 └─ for 每个 priority（从小到大）:
                      ├─ 工厂函数 new 出模板对象
                      └─ obj->DoTiling()   ← 七步流程
                           ├─ GRAPH_SUCCESS        → return 成功
                           ├─ GRAPH_FAILED         → return 失败（终止）
                           └─ GRAPH_PARAM_INVALID  → continue 下一个 priority
            └─ 全部交棒 → OP_LOGE("no valid template") + GRAPH_FAILED
```

七步流程内部的交棒点（u2-l3 已讲顺序，这里强调三态穿透）：

1. `GetShapeAttrsInfo` → 2. `GetPlatformInfo` → 3. `IsCapable()` → 4. `DoOpTiling` → 5. `DoLibApiTiling` → 6. `GetWorkspaceSize` → 7. `PostTiling`

其中第 3 步 `IsCapable()` 返回 `false` 会被基类翻译成 `GRAPH_PARAM_INVALID`；而第 4 步 `DoOpTiling` 里「算到一半发现规格不合适」也可以**直接** `return ge::GRAPH_PARAM_INVALID`——同样会穿透为「本类不支持」。也就是说，三态契约对七步中任何一步都生效。

#### 4.1.3 源码精读

**① 三态语义的定义处。** `DoTiling()` 是一个非虚的「模板方法」：固化七步顺序，最后一步之后统一 `SetTilingKey` 落账：

[ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h:66-102](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L66-L102)

上面这段代码用注释明确写了三态约定（第 67-69 行），第 80-82 行把 `IsCapable()==false` 翻译成 `GRAPH_PARAM_INVALID`，第 99 行在七步全部成功后 `SetTilingKey(GetTilingKey())`。七个纯虚函数声明在同一文件的 [tiling_base.h:111-125](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L111-L125)，子类必须全部实现（不支持的步骤返回 `GRAPH_SUCCESS` 空过）。

**② 轮询循环本体。** 注册表按 `GetNodeType()`（即 OpDef 类名字符串）取出模板表，升序遍历：

[ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h:245-262](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L245-L262)

关键一行是 `if (status != ge::GRAPH_PARAM_INVALID) { return status; }`：`GRAPH_SUCCESS` 和 `GRAPH_FAILED` 都会立刻终止轮询，只有第三态继续。全部交棒则打日志 `no valid template is found` 并返回失败。

**③ 注册的动作。** 工厂函数类型与「一个 priority 只能挂一个类」的防重检查：

[ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h:29-51](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L29-L51)

`TILING_CLASS<T>` 是把「类型」变成「函数」的小技巧：返回一个 lambda 式工厂，`new T(context)`。`AddTiling` 里如果发现同 priority 已被占用，直接报错拒绝——所以**同一算子的两个模板不允许同优先级**，优先级就是它们的唯一次序。

**④ 注册宏：静态全局对象完成自动注册。**

[ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h:322-347](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L322-L347)

注释写明了语义：`priority` 越小优先级越高。宏展开成一个 static 的 `Register` 对象，`.so` 加载时构造，把 `class_name` 的工厂挂进全局注册表——这就是为什么 CMake 只要把 tiling.cpp 编进 `cust_opmaster_rt2.0.so`（u1-l2），算子就自动「被发现」，无需中心化清单。

**⑤ 算子侧的接线。** 以单模板算子 scatter_block_update 为例，三段接线一目了然：

[ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp:78](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L78)

这行代码把 `AiInfraScatterBlockUpdateBaseTiling` 以 priority 1000 注册给算子 `"AiInfraScatterBlockUpdate"`（字符串必须与 OpDef 类名一致）。入口函数与 OpImpl 挂接：

[ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp:418-425](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L418-L425)

[ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp:452-454](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L452-L454)

即：`IMPL_OP_OPTILING` 登记「算子 → 入口函数」，入口函数一行转发进注册表。`TilingParse` 那行是把平台信息（核数、UB 大小）在编译期预解析进 CompileInfo，供运行期 tiling 双来路读取（u2-l3 讲过）。

**⑥ 多模板算子：kv_rms_norm_rope_cache 的三节点链。** 三个类共用一个抽象基类，优先级常量集中定义：

[ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h:123-125](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L123-L125)

三个注册点分列三个文件（`1000/2000/3000`）：

- DS 模板：[ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp:644](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L644)
- D 全载模板：[arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load_tiling.cpp:414-415](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load_tiling.cpp#L414-L415)
- 二分重算模板：[arch35/ai_infra_kv_rms_norm_rope_cache_regbase_recompute_tiling.cpp:319-320](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_recompute_tiling.cpp#L319-L320)

「交棒开关」是 `isRegbase_`（是否 Ascend 950），在基类 `GetShapeAttrsInfo` 里判定：

[ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp:240-251](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp#L240-L251)

三个类的 `IsCapable` 用它互斥分流（DS 是 `!isRegbase_`，两个 arch35 类是 `isRegbase_`）：

[ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp:226-229](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L226-L229)、[arch35/...full_load_tiling.cpp:45-48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load_tiling.cpp#L45-L48)

注意 DS 与 arch35 的 `IsCapable` 恰好互补：在 910B 上 DS 直接成功，两个 arch35 模板根本不会被实例化（轮询已终止）；在 950 上 DS 第 3 步就交棒。

D 全载模板还有一个**第二道交棒点**——`IsCapable` 只看芯片，`DoOpTiling` 算完 UB 预算后如果发现 D 维装不下，直接返回第三态：

[arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load_tiling.cpp:381-384](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load_tiling.cpp#L381-L384)

于是 950 上的完整链路是：DS 交棒（芯片不符）→ 全载尝试（D 装不下 UB 再交棒）→ 二分重算兜底。这正是 u4-l7 说过「DS→D 全载→二分重算」的代码实现。

**⑦ 顺带看清 tiling_util 的一个「占位」事实。** 公共工具里的 regbase 判定目前是硬编码 `return false`：

[ascendc/src/ops-transformer/common/src/tiling_base/tiling_util.cpp:24-41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/src/tiling_base/tiling_util.cpp#L24-L41)

所以 kv 算子没有用它，而是自己在 `GetShapeAttrsInfo` 里对着 `SocVersion::ASCEND950` 判定（见上面的 ⑥）。同文件 [tiling_util.cpp:43-49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/src/tiling_base/tiling_util.cpp#L43-L49) 的 `EnsureNotScalar` 则是把标量形状兜底成 `{1}` 的小工具。教训依旧：**公共设施未必被使用，以各算子实际代码为准**。

**⑧ ops-nn 的平行副本。** matmul 侧没有复用 ops-transformer 这份头，而是有一份自己的 `Ops::NN::Optiling` 命名空间副本（结构几乎相同，另多一个按芯片架构注册的 `REGISTER_TILING_TEMPLATE_WITH_ARCH` 宏，见 [ops-nn/common/inc/op_host/tiling_templates_registry.h:437-440](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/common/inc/op_host/tiling_templates_registry.h#L437-L440)）。用法示例：

[ascendc/src/ops-nn/matmul/ai_infra_matmul/op_host/ai_infra_matmul_tiling.cpp:41-48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/ai_infra_matmul/op_host/ai_infra_matmul_tiling.cpp#L41-L48)

`AiInfraMatmul` 只注册一个 priority 0 的模板，入口同样一行转发。两套 registry 的轮询逻辑一致（[ops-nn 副本:362-379](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/common/inc/op_host/tiling_templates_registry.h#L362-L379)），这是 u4-l8 提过的「两套 common 平行演进」现状。

#### 4.1.4 代码实践

**实践目标**：亲手验证「一个算子注册多个 tiling 类时的选择顺序」，并把 kv 的模板链写成一张可复查的表。

**操作步骤**（纯源码阅读型，无需硬件）：

1. 打开 `tiling_templates_registry.h` 的 `TilingRegistry::DoTilingImpl`（L245-262），抄下轮询终止条件那一行。
2. 用 `grep -rn "REGISTER_TILING_TEMPLATE\|REGISTER_OPS_TILING_TEMPLATE" ascendc/src` 列出全仓库注册点（本文 ⑧ 之前已给出结果），整理成「算子 → 模板类 → priority」三列表。
3. 对照两个算子回答选择顺序：
   - scatter_block_update：只有 priority 1000 一个模板 → 无轮询可言，直接执行。
   - kv_rms_norm_rope_cache：1000（DS）→ 2000（全载）→ 3000（重算），`std::map` 升序即尝试顺序。
4. 为 kv 写出模板链表（参考格式如下，答案见「综合实践」）：

| priority | 模板类 | IsCapable | 交棒条件 | TilingKey 基值 |
| --- | --- | --- | --- | --- |
| （抽象基类） | `...TilingBase` | 恒 `false`（不被注册） | —— | —— |
| 1000 | `...TilingDs` | `!isRegbase_` | 950 上交棒 | 1000~5011 分段 |
| 2000 | `...RegbaseFullLoad` | `isRegbase_` | `ubFactor<=0`（D 装不下 UB）再交棒 | 10000 |
| 3000 | `...RegbaseRecompute` | `isRegbase_` | 兜底，不交棒 | 20000 |

**需要观察的现象**：注册点分散在 4 个文件却汇入同一个 `op_type` 字符串键；三个类共用基类成员（`isRegbase_`、`coreNum_` 等，见 [tiling.h:214-233](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L214-L233)）。

**预期结果**：能不假思索地回答「在 950 上、D=8192 且 UB 放不下时，最终由哪个类完成 tiling？」（答案：Recompute，priority 3000。）

**待本地验证**：若想看运行期轮询日志，需在真机/容器把日志级别开到 DEBUG，观察 `OP_LOGD` 输出的 `Do general op tiling success priority=%d` 行（注册表两处循环都有该日志）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 kv 的 DS 模板 `DoOpTiling` 中某处非法输入的 `return ge::GRAPH_FAILED` 误当成「不支持」使用，会发生什么？
**答案**：`GRAPH_FAILED != GRAPH_PARAM_INVALID`，轮询循环会立即返回失败，2000/3000 两个模板永远不会被尝试——即使它们本可以处理该场景。三态契约要求「不支持」必须用第三态表达。

**练习 2**：为什么 `TilingCases::AddTiling` 要拒绝同一 priority 的重复注册？
**答案**：模板表是 `std::map<int32_t, TilingClassCase>`，priority 既是排序键又是唯一键；若允许覆盖，后注册的会静默顶掉先注册的，导致「改了代码不生效」类难题。直接在注册期报错把问题前移到加载时。

**练习 3**：轮询时每个模板的 `GetShapeAttrsInfo`/`GetPlatformInfo` 会被执行几次？有什么代价？
**答案**：每个被尝试的模板各执行一次（七步在 `DoTiling` 内、以实例为单位运行）。kv 的 `GetShapeAttrsInfo` 含大量 shape 校验，950 上 DS 交棒后全载模板会把同样的校验再做一遍。这是责任链模式的固有代价——公共校验可以下沉到未被注册的抽象基类或 `TilingParse` 阶段缓解（kv 的做法是让三个子类继承基类的校验实现，逻辑只写一份）。

### 4.2 TilingKey 编码：从十进制拼装到手工分段

#### 4.2.1 概念说明

TilingKey 是 host 与 device 之间的「分支暗号」：host 侧算出一个 `uint64_t` 存进 TilingData 头部，device 侧 kernel 入口用 `TILING_KEY_IS(x)` 逐个比对，命中才实例化对应 kernel 模板。它回答的是「**这份 TilingData 应该配哪份 kernel 代码**」。

编码上有两种流派：

1. **公式化拼装**：`tiling_key.h` 提供的 `GET_TILINGKEY` 把若干个枚举段按十进制位拼成一个数。优点是「一眼可解码」（每一位对应一个场景维度），适合段数多、组合规范的算子。
2. **手工分段**：算子自己定义一组常量（如 1000、10000、20000）。优点是直观、改动自由，适合段数少或历史演进的算子。仓库中大多数算子用这种。

无论哪种，铁律只有一条：**host 与 kernel 两侧的数字必须逐位一致**，且编译系统按 key 区分二进制时（`--tiling_key`，见 u6-l4）这份契约还要延伸到构建脚本。

#### 4.2.2 核心流程

`GET_TILINGKEY` 的数学本质是十进制按位编码。设各段取值为 \( d_0, d_1, \dots, d_{n-1} \)（每段必须小于 10），则：

\[ \text{key} = 10^{19} + \sum_{i=0}^{n-1} d_i \cdot 10^{i} \]

- 低位是第一段，高位向左扩展，每段占一个十进制位；
- 偏移 \( 10^{19} \)（`TILINGKEYOFFSET`）把「公式化 key」整体搬到 \(10^{19}\) 以上的取值区间，与手工编码的小数值（千、万级）天然隔离，避免撞车；
- 解码就是逐位取十进制位。

host 侧落账与 device 侧对号：

```text
host: DoTiling() 第 99 行  context_->SetTilingKey(GetTilingKey())   ← 落账
device: kernel 入口        TILING_KEY_IS(10000) → GET_TILING_DATA_WITH_STRUCT(对应 TilingData 类型)
```

#### 4.2.3 源码精读

**① 拼装公式的实现。** 递归模板在编译期完成十进制累加：

[ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h:23-32](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h#L23-L32)

[ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h:48-59](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h#L48-L59)

`RecursiveSum(d0, d1, d2)` = `d0 + 10*(d1 + 10*d2)`，即 `d0 + 10·d1 + 100·d2`。文件头 [tiling_key.h:34-46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h#L34-L46) 的注释给出了 FlashAttention 类算子的六段位域约定（Ub0/Ub1/Block/DataType/Format/Sparse，从低位到高位各占一个十进制位）——这是「公式化流派」的说明书。

**② 手工分段的活例子。** scatter 只有一个 key：

[ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp:59](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L59)

在 `DoOpTiling` 末尾赋值（[L350](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L350)）。

kv 则是「一套决策树编出 18 个手工分段 key」：DS 模板内定义了 `TLING_KEY_1000/1001/1010/1011/2000/.../5011` 一组常量（[ds_tiling.cpp:40-56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L40-L56)，位数对应 dtype/布局/MTP/量化等维度），而两个 arch35 模板各只占一个基值：

[ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h:211-212](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L211-L212)

**③ 「算子名_key」命名约定：把 TilingData 类型绑到 key 上。** 这是本讲最值得学的一手。host 侧用 `REGISTER_TILING_DATA_CLASS` 注册 TilingData 结构时，直接把 key 编进注册名：

[ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h:49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L49)（DS 用主名）

[ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h:90](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L90)、[tiling.h:121](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L121)（全载=10000、重算=20000）

`AiInfraKvRmsNormRopeCache_10000` 这个名字里的 10000，正是 kernel 侧要匹配的数字。kernel 入口的镜像分支：

[ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache.cpp:67-76](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache.cpp#L67-L76)

`TILING_KEY_IS(10000)` 命中就用 `GET_TILING_DATA_WITH_STRUCT(AiInfraKvRmsNormRopeCacheRegbaseFullLoadTilingData, ...)` 解包全载版结构体；同一个 kernel 入口的后续分支（如 [L88-95](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache.cpp#L88-L95)）则统一解包 DS 版 `AiInfraKvRmsNormRopeCacheTilingData`。三份 TilingData 结构互不相同（[tiling.h:25-47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L25-L47) DS 版、[L52-88](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L52-L88) 全载版、[L93-119](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L93-L119) 重算版）——**模板链上的每个节点，都带着自己的 TilingData 方言**。

**④ 警惕「key 撞车」。** 由于手工分段没有全局登记处，不同算子间 key 可以重复（scatter 的 1000 与 kv DS 的 1000 互不干扰，因为 key 只在单算子内比较），但**同一算子内**必须互斥；而公式化 key 靠 \(10^{19}\) 偏移与手工值隔离。若同一算子同时存在两种流派的 key，就要人工保证不重叠。

#### 4.2.4 代码实践

**实践目标**：体验两种编码流派，并验证「host 注册名 = kernel 分支号」的镜像契约。

**操作步骤**：

1. 心算一个公式化 key：`GET_TILINGKEY(2, 3, 1)` 展开是什么？
2. 打开 kv 的 kernel 入口文件，`grep -n "TILING_KEY_IS" ai_infra_kv_rms_norm_rope_cache.cpp`，统计分支总数；
3. 回到 host 侧 tiling.h，核对 `_10000`/`_20000` 两个 `REGISTER_TILING_DATA_CLASS` 名与 kernel 第 67/75 行的数字是否一致；
4. （进阶）对照 ds_tiling.cpp 的 `TLING_KEY_*` 常量与 kernel 中的 `AI_INFRA_KV_RMS_NORM_ROPE_CACHE_B16_NORM` 等宏名（宏定义在 kernel 侧头文件），挑 3 个 key 画「host 决策条件 → key → kernel 分支」对照行。

**需要观察的现象**：host 常量、注册名后缀、kernel 宏值三处数字完全一致；DS 的多个 key 共用一份 TilingData 结构，而 arch35 两个 key 各用一份。

**预期结果**：第 1 步答案为 \(10^{19} + 2 + 3\times10 + 1\times10^2 = 1000000000000000132\)。第 2 步分支数应与 u4-l7 说的「18 个分段 key + 2 个 arch35 key」吻合（以你 grep 的实际计数为准）。

**待本地验证**：第 4 步若想看真实 key 值，可在 NPU 环境跑 ST 用例并用 `torch_npu profiler` 或 DEBUG 日志观察下发的 tiling key（本仓库脚本层面未直接打印 key，需借助 CANN 工具）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GET_TILINGKEY` 要加 \(10^{19}\) 偏移，而不是从 0 开始？
**答案**：手工分段 key 都是千、万级小数值。把公式化 key 整体搬到 \(10^{19}\) 以上区间，两种流派即使混用在同一算子也不会撞车；同时 \(10^{19}\) 以上的数值本身就是「这是拼装出来的 key」的信号。

**练习 2**：kv 的 DS 模板有约 18 个 key 却只注册了一份 TilingData（主名注册），这样安全吗？依据是什么？
**答案**：安全。`GET_TILING_DATA_WITH_STRUCT` 解包时只关心**结构体布局**，不关心 key 值；DS 的 18 个 key 都由同一个 `DoOpTiling` 决策树产出、共用同一份 `AiInfraKvRmsNormRopeCacheTilingData`（kernel 入口 L88 起的多个分支解包同一结构体可证）。key 在这里只用于选择 kernel 模板实例，不区分 TilingData 方言。

**练习 3**：MoE（u4-l5）用 \(10^6 + \text{sortMode}\times10^5 + \dots\) 的位段编码，与 `GET_TILINGKEY` 的编码在数学上有何关系？
**答案**：同构。两者都是 \(\sum d_i \cdot 10^{i}\) 的十进制按位编码，区别只在基值（MoE 用 \(10^6\) 起步的手写表达式，`GET_TILINGKEY` 用 \(10^{19}\) 偏移的编译期模板）与段宽约定（MoE 某些段取值可到 9 以上时需自行保证不进位越界）。理解了本讲公式，u4-l5 的 MoE key 与 u4-l1 的 FIA 18 位编码都可举一反三。

### 4.3 tiling 缓存：MurmurHash 与 TilingCache

#### 4.3.1 概念说明

matmul 类算子的 tiling 特别「重」：要综合 M/N/K、transpose、dtype、bias、量化模式、L2 分块等一堆输入做决策（u4-l8 讲过 L2 cache tiling 与 simplified_key）。而推理服务的请求形状高度重复——同样的 (M,N,K) 组合每秒出现成百上千次。既然输入相同、平台相同，切分方案必然相同，**每次重算 tiling 就是纯浪费**。

对策是经典的三件套缓存设计：

1. **键压缩**：把「影响 tiling 的全部输入」压成一个 32 位哈希（`MurmurHash`）；
2. **防冲突**：哈希命中后，再用完整输入做一次相等性比对（`TilingCache::Get`），杜绝两个不同输入撞出同一哈希时取错方案；
3. **并发安全 + 容量上限**：`std::shared_mutex` 读写锁 + 最多 500 条，写满即拒绝新增（防内存膨胀），不做淘汰。

必须诚实说明的一点：在当前 HEAD，`ops-nn/matmul/common/op_host/op_tiling/` 下的 `tiling_cache.h` 与 `hash.cpp` **在仓库内没有检索到实例化调用点**（全仓库 grep `TilingCache<`/`MurmurHash(` 仅命中定义处及 `ops-nn/common` 下的平行副本）——它们是随公共库搬运进来的预留基建。matmul 当前实际在用的缓存路径是 `TilingParse` 阶段的 `TilingPrepareForOpCache`（调优知识库缓存），见本模块末尾。读代码时要能区分「存在的基础设施」与「实际执行的路径」。

#### 4.3.2 核心流程

一次「带缓存的 tiling」理想流程：

```text
输入 (M,N,K, transpose, dtype, ...) 到达
  ├─ 拼 HashInput → key = MurmurHash(HashInput 内存, seed)
  ├─ cache.Get(key, hash_input, item)
  │    ├─ 命中且全量比对一致 → 直接用 item（跳过整个 DoOpTiling）
  │    └─ 未命中 / 比对不一致 → 走完整 tiling → cache.Add(key, hash_input, 结果)
  └─ 下发
```

MurmurHash3 x86_32 的核心三步：

1. 每读 4 字节为一组：`k *= c1; k = rotl(k,15); k *= c2; h ^= k; h = rotl(h,13); h = h*5 + 0xe6546b64;`
2. 尾部不足 4 字节按字节拼入后同样 scramble；
3. 终局雪崩（avalanche）：`h ^= len; h ^= h>>16; h *= 0x85ebca6b; h ^= h>>13; h *= 0xc2b2ae35; h ^= h>>16;`

#### 4.3.3 源码精读

**① MurmurHash 实现。** 逐 4 字节主体循环 + 尾部处理 + 终局雪崩，一应俱全：

[ascendc/src/ops-nn/matmul/common/op_host/op_tiling/hash.cpp:29-67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/common/op_host/op_tiling/hash.cpp#L29-L67)

其中 `MurmurScramble`（L29-35）封装了 `*=c1; rotl15; *=c2` 三连击；L43-50 是主体循环（`len >> 2` 组、每组 4 字节）；L52-57 处理尾部 `len & 3` 个字节；L60-65 是终局雪崩。常数 `c1=0xcc9e2d51`、`c2=0x1b873593` 与旋转量都是 MurmurHash3 的标准参数。

**② TilingCache：防冲突比对是灵魂。**

[ascendc/src/ops-nn/matmul/common/op_host/op_tiling/tiling_cache.h:24-79](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/common/op_host/op_tiling/tiling_cache.h#L24-79)

三个方法分工：

- `Add`（[L27-42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/common/op_host/op_tiling/tiling_cache.h#L27-L42)）：写锁；容量到 `kMaxTilingCacheEntryNum`（500，L22）即拒绝；已存在则不覆盖。
- `Replace`（[L44-57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/common/op_host/op_tiling/tiling_cache.h#L44-L57)）：同 key 覆盖写。
- `Get`（[L59-73](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/common/op_host/op_tiling/tiling_cache.h#L59-L73)）：读锁；先查 map，再执行关键的 `hash_input == iter->second.input()` 全量比对——**哈希只做初筛，相等性由原文保证**。这要求 `HashInput` 类型重载了 `operator==`（`HashItem` 需提供 `input()` 访问器）。

注意这是「有界、不淘汰」策略：500 条写满后新形状永远进不了缓存，命中的永远命中。对推理场景（形状集合有限且稳定）这是够用且实现最简的选择；对形状千变万化的训练场景就需要 LRU 之类淘汰策略了——这也侧面说明这套基建是为推理形态设计的。

**③ matmul 实际在用的缓存路径。** `AiInfraMatmul` 的 `TilingParse` 阶段调用 `TilingPrepareForOpCache`：

[ascendc/src/ops-nn/matmul/ai_infra_matmul/op_host/ai_infra_matmul_tiling.cpp:81-84](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/ai_infra_matmul/op_host/ai_infra_matmul_tiling.cpp#L81-L84)

而 `TilingPrepareForOpCache` 的实现是**运行期到 legacy 公共库里取函数指针**：

[ascendc/src/ops-nn/common/src/op_host/op_cache_tiling.cpp:22-33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/common/src/op_host/op_cache_tiling.cpp#L22-L33)

`LegacyCommonMgr::GetFunc("LegacyTilingPrepareForOpCache")` 的模式与 u3-l2 讲过的 dlopen/dlsym 六级查找同族：本仓库只留壳，真正的 matmul 调优缓存（含 runtime_kb.json 知识库查询，u4-l8）在预编译的 legacy 公共库里。同文件 [op_cache_tiling.cpp:48-63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/common/src/op_host/op_cache_tiling.cpp#L48-L63) 的 `GenTiling` 同样委托 `LegacyGenTbeMatmulTiling`。

**④ 为什么 ops-transformer 侧没有这层缓存？** attention/posembedding 类算子的 tiling 计算量小（形状检查 + 少量除法），且轮询机制本身把每个模板的公共计算压进了基类；而 matmul 的 tiling 涉及多轮迭代搜索，计算成本高、复用收益大。缓存是「按需引入」的优化，不是框架标配。

#### 4.3.4 代码实践

**实践目标**：用最小程序验证 MurmurHash 的分布与碰撞概率直觉，并设计一个防冲突比对被触发的构造性用例。

**操作步骤**：

1. 把 `hash.cpp` 的 `MurmurHash` 抄进一个独立 `main`（连同 `MurmurScramble` 与常数），编译为本地 x86 程序（**示例代码**，非仓库原有，仓库文件依赖 CANN 头不便脱离环境编译）：

```cpp
// 示例代码：仅用于本地理解算法，与仓库实现等价
#include <cstdio>
#include <cstdint>
int main() {
    uint32_t shapes[] = {4096, 4096, 4096};   // 模拟 (M,N,K)
    uint32_t h = MurmurHash(shapes, sizeof(shapes), 0);
    printf("key = %u\n", h);
}
```

2. 对几组相近输入（`{4096,4096,4096}`、`{4096,4096,4095}`、`{8192,1024,512}`）各算一次，观察输出是否毫无规律；
3. 构造碰撞场景的思考实验：假如两个不同输入碰巧同 key，`TilingCache::Get` 的哪一行救了场？

**需要观察的现象**：相近输入的哈希值完全不同（雪崩效应）；step 3 中 `hash_input == iter->second.input()` 返回 false，`Get` 返回 false，调用方老老实实重算 tiling——**宁可慢，不可错**。

**预期结果**：step 2 打印三个互不相关的 32 位数。碰撞在 3~4 个输入间几乎不可能出现（32 位空间约 43 亿），但在高并发长期运行的缓存里是统计必然，这正是防冲突比对存在的原因。

**待本地验证**：示例程序需在本地 g++ 环境编译运行；仓库内该函数当前无调用点，无法在 NPU 环境直接观测其缓存命中行为。

#### 4.3.5 小练习与答案

**练习 1**：`TilingCache` 为什么用 `std::shared_mutex` 而不是 `std::mutex`？
**答案**：tiling 查询是典型读多写少——每个推理请求都要 `Get`，只有新形状首次出现才 `Add`。`shared_mutex` 允许多个 `Get` 并发共享读锁，`mutex` 会把所有读串行化，高并发下成为瓶颈。

**练习 2**：缓存上限 500 写满后 `Add` 静默返回，这有什么风险？如果换成 LRU 要改哪些地方？
**答案**：风险是形状集合超过 500 的负载里，后出现的新形状永远无法被缓存，收益封顶（但正确性不受影响，只是退化为每次重算）。改 LRU 需要：把 `std::map` 换成 `std::list` + `std::unordered_map` 的 LRU 结构（或利用 map 有序性按时间戳淘汰），`Get` 命中时要刷新新旧序（因此 `Get` 也不再是纯共享读，需升级为写锁或引入细粒度锁）。

**练习 3**：如果只缓存 32 位哈希、不存 `hash_input` 原文做比对，最坏会发生什么？
**答案**：哈希碰撞时会把 A 形状的 tiling 方案当成 B 形状的返回——错误的分块参数下发 kernel，轻则结果错乱、重则越界写。这就是「哈希只做初筛、相等性由原文保证」这条铁律的由来。

## 5. 综合实践

**任务：为 `ai_infra_kv_rms_norm_rope_cache` 产出一份完整的《tiling 模板链档案》，并横向对比单模板算子。** 这是本讲三个模块的综合应用，产物可直接当作你日后接入手新模板时的 checklist。

**步骤**：

1. **登记注册点**。在 `op_host/` 及 `op_host/arch35/` 下 grep `REGISTER_OPS_TILING_TEMPLATE`，记录每个注册的（op_type、类名、priority、所在文件行号）。应得到 4.1.4 表格的三行数据（1000/2000/3000）。
2. **画出继承与交棒图**。以 `AiInfraKvRmsNormRopeCacheTilingBase`（[tiling.h:214](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L214)）为根，标出三个子类（[L288](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L288)、[L321](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L321)、[L350](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L350)），在每个子类旁注两件事：`IsCapable` 的条件、除 `IsCapable` 外的第二个交棒点（如果有）。参考答案：

   - **DS（1000）**：`IsCapable = !isRegbase_`；无第二交棒点（校验失败属 `GRAPH_FAILED` 硬错误）。
   - **全载（2000）**：`IsCapable = isRegbase_`；第二交棒点在 `DoOpTiling` 内 `ubFactor <= 0` 时返回 `GRAPH_PARAM_INVALID`。
   - **重算（3000）**：`IsCapable = isRegbase_`；兜底无交棒点。
3. **补全 key 与 TilingData 映射**。为链上每个节点记录：key 基值、`REGISTER_TILING_DATA_CLASS` 注册名、kernel 侧 `TILING_KEY_IS` 行号（10000→kernel L67，20000→kernel L75，DS 各 key→L88 起）。
4. **模拟三个场景**并写出最终选中的模板与 key：
   - 910_93、B1SD 布局、无量化 → **DS**，key 落在 1000 段；
   - 950（regbase）、dk+dv 总量小于 UB 预算 → **全载**，key=10000；
   - 950、D 极大（全载 `ubFactor<=0`）→ **重算**，key=20000。
5. **横向对比**：用同样方法归档 scatter_block_update（单模板 priority 1000、单 key 1000），回答「什么规模的算子才值得拆多模板？」——参考结论：当且仅当存在**互斥的硬件代次或规格区间**需要不同切分策略时；否则单模板 + key 分段即可（MoE、FIA 的多 key 单模板/自建注册表是另外两种中间形态）。
6. **（可选，需环境）** 用 `bash build.sh -u --ophost` 跑 kv 的 tiling UT（u6-l1 将详述该框架），在用例里断言不同 shape 下 `GetTilingKey()` 的返回值与你的档案一致。**待本地验证**。

## 6. 本讲小结

- `TilingBaseClass::DoTiling()` 的三态返回值是模板轮询的引擎：`GRAPH_SUCCESS` 终止轮询、`GRAPH_FAILED` 终止整个流程（不会重试）、`GRAPH_PARAM_INVALID` 交棒给下一优先级；七步中任何一步都能发起交棒（`IsCapable==false` 被基类翻译、`DoOpTiling` 可直接返回）。
- 注册表 = `std::map<priority, 工厂函数>` 升序遍历；注册靠宏展开的 static 全局对象在 `.so` 加载期自动完成，同 priority 重复注册会在加载期报错；ops-transformer 与 ops-nn 各持一套平行副本，attention 族（FIA）另起炉灶。
- kv_rms_norm_rope_cache 是多模板的范本：抽象基类（不注册、`IsCapable` 恒 false）+ DS(1000) + 全载(2000) + 重算(3000)，靠 `isRegbase_` 与 UB 预算两级交棒完成「新硬件优先尝试激进策略、装不下再退」的调度。
- TilingKey 编码有两派：`GET_TILINGKEY` 的十进制按位拼装（\(10^{19}\) 偏移防撞车）与算子手工分段；「`REGISTER_TILING_DATA_CLASS(算子名_key, ...)`」命名约定把 TilingData 类型显式绑到 key，与 kernel `TILING_KEY_IS` 形成可 grep 验证的镜像契约。
- ops-nn 的 `TilingCache`（读写锁 + 500 上限 + 全量比对防冲突）与 `MurmurHash` 是为 matmul 类重 tiling 预留的缓存基建，当前 HEAD 仓库内无实例化调用点；matmul 实际在用的缓存是 `TilingParse` 阶段经 `op_cache_tiling.cpp` 委托 legacy 公共库的调优缓存。

## 7. 下一步学习建议

本讲是第 5 单元（高级机制）的开篇。建议按以下顺序继续：

1. **u5-l2（AIV/AIC 协同与 FlashDecode）**：tiling 决定的分核方案如何在 kernel 侧落地为向量核/立方核的并行与跨核同步——本讲的 `CalcTschBlockDim`（[tiling_base.h:127-135](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L127-L135)）是两者的衔接点。
2. **u5-l4（Tiling Sink 与 AICPU）**：当 Host 侧 tiling 读不到设备张量数值时，整个 tiling 计算如何下沉到 AICPU 执行——可视为对本讲 Host 侧框架的「反叛」。
3. 想动手验证模板轮询与 key 断言的读者，提前浏览 `ascendc/src/tests/ut/framework_normal` 的 `tiling_context_faker`（u6-l1 的主菜），那里能纯 CPU 复现本讲的全部选择逻辑。
