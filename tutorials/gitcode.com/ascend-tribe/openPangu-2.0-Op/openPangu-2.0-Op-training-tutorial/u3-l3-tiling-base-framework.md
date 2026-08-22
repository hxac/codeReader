# tiling_base 框架：TilingBase 责任链与模板注册

## 1. 本讲目标

在 u2-l3 中，我们以 `ai_infra_aggregate_hidden` 为标本读懂了一个「单实现」的 tiling 函数：它把校验、切分、写 TilingData 全部写在一个 `TilingXxx` 函数里。但当你打开 FlashAttention 这类复杂算子的 op_host 目录，会发现 tiling 代码被拆成了七八个文件、十几个类——它们靠什么组织起来？答案就是本讲的主角：`common/include/tiling_base` 框架。

学完本讲，你应该能够：

1. 解释 `TilingBase` 基类的「模板方法」执行框架，以及 `GRAPH_SUCCESS / GRAPH_FAILED / GRAPH_PARAM_INVALID` 三态返回值各自的调度语义。
2. 说明 `tiling_templates_registry` 如何用「算子名 + 芯片版本 + 优先级」三个键注册多个候选 tiling 实现，并按责任链顺序逐个尝试。
3. 读懂 `tiling_key.h` 的十进制位组装 tilingKey 规则，理解它与 kernel 侧 `TILING_KEY_IS` 分支、以及编译产物（多份二进制）的关系。
4. 使用 `tiling_util` 与 `data_copy_transpose_tiling` 两个公共小工具，避免重复造轮子。
5. 对照 sinkhorn（单实现）与 FlashAttention（六模板链）两个真实落地，画出一次 tiling 请求的完整执行链。

## 2. 前置知识

本讲需要以下基础，不熟悉的术语用大白话解释一遍：

- **Tiling 的四项契约**（u2-l3）：tiling 是 Kernel 启动前 Host 侧的「作战规划」，产出 blockDim、tilingKey、TilingData 字节流、workspace 大小四样东西。本讲不重复切分算法本身，只讲「tiling 代码如何被组织与调度」。
- **责任链模式（Chain of Responsibility）**：想象客服系统——一级客服答不了就转二级，二级不行转专家，任何一级能处理就结束。本讲中，每个 tiling 类就是一级客服：`IsCapable()`（我能不能处理）返回 false 就换下一家。
- **模板方法模式（Template Method）**：基类把「做菜的流程」固定为 洗菜→切配→下锅→装盘，子类只实现每一步的具体做法。`TilingBase::DoTiling()` 就是固定流程，七个纯虚函数就是子类要填的空。
- **注册表 + 工厂函数**：程序启动时，各翻译单元里的全局对象把「类名 → 构造函数指针」塞进一张全局表；运行期按 key 查表构造。C++ 里靠 **static 全局变量在 main 之前完成注册** 这一技巧实现。
- **`std::map` 的有序性**：`std::map<int32_t, T>` 按 key 从小到大遍历——这正是「priority 越小越优先」的实现基础。
- **为什么一个算子需要多个 tiling 实现**：同一个 FlashAttention 算子要支持 BSH/BSND/SBH 多种布局、变长/定长两种序列、带/不带 dropout 等众多场景。如果塞进一个函数，分支组合会爆炸；拆成多个「特化模板类」，每个只服务一种场景，互不干扰，还能按场景选择最优切分。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| [ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h) | `TilingBase` 抽象基类与执行框架 | 模板方法 + 三态返回值 |
| [ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h) | 优先级注册表与责任链调度器 | 两套注册表（带/不带 socVersion） |
| [ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h) | tilingKey 十进制位组装规则 | Host 写、Device 读的分支信号编码 |
| [ascendc/src/ops-transformer/common/src/tiling_base/tiling_util.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_base/tiling_util.cpp) | tiling 公共小工具（同名头文件 `tiling_util.h`） | socVersion 判断、标量 shape 保护 |
| [ascendc/src/ops-transformer/common/include/tiling_base/data_copy_transpose_tiling.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/data_copy_transpose_tiling.h) | 转置搬运 tiling 参数打包 | FA 布局转换的公共件 |
| [ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling_base.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling_base.cpp) | sinkhorn 的 tiling 入口（对接注册表） | 单实现落地样板 |
| [ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp) | sinkhorn 的 `TilingBase` 子类实现 | 七个钩子的完整填空示范 |
| [ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp) | FA 的 tiling 入口 | 多模板链落地样板 |
| [ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp) | FA 的 tiling 基类与六个特化模板 | 责任链实战 |
| [ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling_common.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling_common.h) | FA 的 CompileInfo 结构体 | 与框架 `CompileInfoCommon` 的对应关系 |

## 4. 核心概念与源码讲解

### 4.1 TilingBase 基类：模板方法与三态返回值

#### 4.1.1 概念说明

`TilingBase` 是所有「框架化」tiling 实现的抽象基类。它解决两个问题：

1. **固化执行流程**：任何 tiling 都要经历「取平台信息 → 取输入属性 → 判断能否处理 → 算切分 → 算高阶 API tiling → 算 workspace → 落盘 TilingData」这一套流程。基类把顺序写死，子类只填内容，不会漏步骤，也保证所有算子的 tiling 行为一致（例如统一由基类在最后调用 `SetTilingKey`）。
2. **定义「让位」协议**：单实现时代（u2-l3 的 aggregate_hidden）tiling 函数只有 成功/失败 两种结局；多模板时代需要第三种——「我这级处理不了，请找下一级」。这就是三态返回值中的 `GRAPH_PARAM_INVALID`。

注意区分三态的语义（这是本讲最重要的概念）：

| 返回值 | 含义 | 框架行为 |
| --- | --- | --- |
| `GRAPH_SUCCESS` | 本类完成 tiling | 立即返回成功，**不再尝试后续类** |
| `GRAPH_FAILED` | 发生真正的错误（参数非法、平台异常等） | 立即中止**整条链**，tiling 失败 |
| `GRAPH_PARAM_INVALID` | 本类不支持当前输入场景 | 忽略本类，**继续尝试下一优先级的类** |

#### 4.1.2 核心流程

`DoTiling()` 的固定流水线（伪代码）：

```text
DoTiling():
    ret = GetShapeAttrsInfo()      # 2.先取输入/输出/属性信息（可做参数校验）
    if ret != SUCCESS: return ret  #   失败 → 整链中止
    ret = GetPlatformInfo()        # 1.再取平台信息（核数、UB/L1/L0 大小）
    if ret != SUCCESS: return ret
    if not IsCapable():            # 本类能力是否覆盖当前场景
        return GRAPH_PARAM_INVALID #   不覆盖 → 让位给下一优先级类
    ret = DoOpTiling()             # 3.计算数据切分（CoreSplit 等）
    ret = DoLibApiTiling()         # 4.计算高阶 API（如 Matmul）的 tiling
    ret = GetWorkspaceSize()       # 6.计算 workspace
    ret = PostTiling()             # 7.SetBlockDim + 保存 TilingData
    context_->SetTilingKey(GetTilingKey())  # 5.基类统一写 tilingKey
    return GRAPH_SUCCESS
```

两个容易忽略的细节：

- **步骤顺序是「先 shape 后平台再能力判断」**，所以 `IsCapable()` 里可以放心使用前两步填好的成员（FA 的 `IsCapable` 就在比对 inputParams 与模板约束）。
- **`SetTilingKey` 由基类在最后统一执行**，子类的 `PostTiling` 里不需要、也不应该再调一次；这与 u2-l3 单实现风格（tiling 函数自己 `SetTilingKey`）不同，读代码时要分清风格。

#### 4.1.3 源码精读

三态语义的官方注释写在 [ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h:L77-L80](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L77-L80)，明确说了 `GRAPH_PARAM_INVALID` 表示「本类不支持，需要继续往下执行其他 Tiling 类的实现」。

执行框架本体在 [ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h:L81-L113](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L81-L113)：这段代码就是上面伪代码的原型——依次调用七个虚函数钩子，任何一步非 `GRAPH_SUCCESS` 都提前返回；唯一「合法的失败」是 `IsCapable()` 为 false 时返回 `GRAPH_PARAM_INVALID`（L91-L93）。注意 L110 的 `context_->SetTilingKey(GetTilingKey())`：`GetTilingKey()` 是七个钩子中唯一的 const 函数，只算不算写。

七个纯虚钩子的声明在 [ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h:L121-L136](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L121-L136)，每行注释就是各步职责：`IsCapable` / `GetPlatformInfo` / `GetShapeAttrsInfo` / `DoOpTiling` / `DoLibApiTiling` / `GetTilingKey` / `GetWorkspaceSize` / `PostTiling`。子类必须全部实现（C++ 纯虚函数），不存在「可选钩子」。

基类还替子类保管公共状态，见 [ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h:L209-L215](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L209-L215)：`context_`（gert::TilingContext 指针）、`ascendcPlatform_`、`blockDim_`、`workspaceSize_`、`tilingKey_`、`aicoreParams_`（L35-L43 定义的结构体，装 UB 大小与核数）。配套的两个 CompileInfo 结构体 `CompileInfoCommon`（L45-L57）与 `FlashAttentionScoreGradEnhanceCompileInfo`（L58-L69）存「编译期缓存」的平台快照，后文 4.2 会看到它们在 UT 场景的妙用。

辅助工具 `CalcTschBlockDim` 在 [ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h:L138-L146](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L138-L146)：当 AIC 数不超过 AIV 数时，按 `aivCoreNum / aicCoreNum` 的比值把 cube 切片数折算成混合调度（TSCH）下的 blockDim——cube/vector 混合下发时一个「调度块」要占多个 vector 核。

子类怎么填空？以 sinkhorn 为例（下一节会讲它的注册），类声明在 [ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:L79-L141](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L79-L141)：`SinkhornTilingBase` 继承 `Ops::Transformer::OpTiling::TilingBase`，七个 override 一一对应基类钩子，另加私有的 `CheckInputShape/CheckOutputShape/SplitCores` 等具体实现。它的 `IsCapable()` 恒返回 true（L95-L98）——因为整条链只有它一个实现，无需让位。`PostTiling()`（L546-L554）集中展示了落盘三件套：`SetBlockDim(needCoreNum)`、`GetWorkspaceSizes(1)[0] = workspaceSize_`、`tilingData_.SaveToBuffer(...) + SetDataSize(...)`，与 u2-l3 总结的四项契约完全吻合。

#### 4.1.4 代码实践

**实践目标**：把「基类钩子 ↔ 子类实现」的对应关系亲手对一遍，验证模板方法模式的真实落点。

**操作步骤**（源码阅读型，无需 NPU）：

1. 打开 [tiling_base.h:L121-L136](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L121-L136)，抄下七个纯虚函数名。
2. 打开 sinkhorn 的 [tiling.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp)，为每个钩子找到实现体的起止行号，填成一张表。
3. 检查每个实现体内部：是否还有子步骤（如 `DoOpTiling` 调 `CheckInputShape → CheckOutputShape → CheckOptionalOutputShape → SplitCores`，见 L491-L532）。
4. 回答附加题：如果 `GetShapeAttrsInfo()` 返回 `GRAPH_FAILED`，`IsCapable()` 还会被调用吗？

**需要观察的现象**：七个钩子在子类中全部有 override；没有任何一个子类钩子直接调用 `context_->SetTilingKey`。

**预期结果**（笔者已核对，读者可复验）：`IsCapable` L95-L98、`GetPlatformInfo` L143-L155、`GetShapeAttrsInfo` L157-L227、`DoOpTiling` L491-L532、`DoLibApiTiling` L534-L537（空实现直接返回 SUCCESS）、`GetTilingKey` L556-L561、`GetWorkspaceSize` L539-L544、`PostTiling` L546-L554。附加题答案：不会——`DoTiling()` 在 L84-L86 就提前 return 了，这正是「FAILED 中止整链」的体现。

#### 4.1.5 小练习与答案

**练习 1**：`DoLibApiTiling` 这个钩子存在的意义是什么？为什么 sinkhorn 的实现是空的？

答案：它负责「高阶 API」（如 MatmulApiTiling 这类 CANN 库算子）的 tiling 参数计算。sinkhorn 是纯向量算子，不用 Matmul，所以返回 `GRAPH_SUCCESS` 即可（L534-L537）。FA 这类 cube 算子会在里面填 bmm1/bmm2 的 tiling。

**练习 2**：基类成员 `context_` 为什么是 protected 而不是 private？这样设计的代价是什么？

答案：protected 让子类钩子能直接访问 `TilingContext`（取 shape、写 tilingData 都离不开它），减少一层封装。代价是所有子类与基类形成白盒耦合，基类无法约束子类对 context 的写入顺序——框架靠「约定」而非「编译器」保证流程正确。

**练习 3**：子类构造函数里为什么要调一次 `Reset()`（sinkhorn L81-L85）？

答案：因为注册表每次尝试都会 new 一个新实例（见 4.2），成员本应是干净的；但 `Reset()` 把成员恢复到已知初值（L563-L578），防止同一实例被复用（框架提供 `Reset(context)` 入口，L88-L92）时残留上一帧的状态。这是防御式编程，不是必需路径。

### 4.2 tiling_templates_registry：优先级注册表与责任链调度

#### 4.2.1 概念说明

有了 `TilingBase`，还差两个问题：一帧请求到来时**由谁创建类实例、按什么顺序尝试**；以及**注册发生在什么时候**。`tiling_templates_registry.h` 给出答案：

- **工厂函数指针** `TilingClassCase`：`std::unique_ptr<TilingBase> (*)(gert::TilingContext*)`——注册的不是对象，是「造对象的函数」。
- **`TilingCases`**：单个算子的候选表，`std::map<int32_t, TilingClassCase>`，key 是优先级，map 升序遍历即责任链顺序。
- **两套注册表**：`TilingRegistry`（只按算子名索引）与 `TilingRegistryNew`（按「芯片版本 + 算子名」两级索引）。后者让同一算子在不同芯片上挂不同的模板集合——编译期 `AddConfig`（u2-l2）之外的另一层芯片适配。
- **static 全局变量注册**：`REGISTER_*` 宏展开成一个全局对象，其构造函数在 `main` 之前执行注册，无需手工调用任何 init 函数。

#### 4.2.2 核心流程

一帧 tiling 请求的调度过程（以带 socVersion 的 `TilingRegistryNew` 为例）：

```text
CANN 框架按 op_type 找到 IMPL_OP_OPTILING 注册的入口函数（如 TilingForSinkhorn）
        │
        ▼
入口函数调用 TilingRegistryNew::GetInstance().DoTilingImpl(context)
        │
        ├─ 1. 确定 socVersion：
        │     platformInfo != null → PlatformAscendC(platformInfo).GetSocVersion()   # 真实硬件/ST
        │     platformInfo == null → CompileInfoCommon(context->GetCompileInfo())->socVersion  # UT 伪造上下文
        │
        ├─ 2. 查表：registry_map_[soc_version][op_type] → map<priority, 工厂函数>
        │
        └─ 3. 按 priority 升序遍历：
                实例 = 工厂函数(context)          # 每次尝试都 new 一个新对象
                status = 实例->DoTiling()
                status == GRAPH_PARAM_INVALID → continue（试下一个）
                否则 → return status              # SUCCESS 或 FAILED 都到此为止
              全部让位 → OP_LOGE + return GRAPH_FAILED
```

注意第 1 步的双通道 socVersion 探测：**真实环境走 platformInfo，UT 环境走 CompileInfo**。这正是 u8 单元要讲的 UT 框架能在无硬件环境回放 tiling 的关键接缝之一。

#### 4.2.3 源码精读

工厂模板函数 `TILING_CLASS` 与函数指针类型在 [ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h:L29-L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L29-L35)：把「任意 TilingBase 子类」规约成统一签名的构造器。

`TilingCases::AddTiling` 在 [L42-L51](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L42-L51)：以 priority 为 key 插入 map；若 priority 已存在则打日志并 return——**同优先级先注册者胜，后来者被静默忽略**（只留一条 OP_LOGE，不覆盖、不报错终止）。

责任链调度核心在 `TilingRegistryNew::DoTilingImpl(context)`，见 [L97-L131](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L97-L131)：L99-L116 完成 socVersion 双通道探测（platformInfo 为空时强转 `CompileInfoCommon` 读 `socVersion`，L103-L107；探测到 `RESERVED_VERSION` 视为失败，L112-L115）；L117-L128 是链遍历——`status != ge::GRAPH_PARAM_INVALID` 即返回（L122-L125），只有 PARAM_INVALID 才落日志继续（L126）；走完全链仍无果则 L129-L130 报「no valid template is found」。另有一个**显式优先级列表**重载 [L133-L166](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L133-L166)，允许调用方只尝试指定优先级的子集。

注册表本体是 [L183](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L183) 的 `registry_map_`：`map<soc_version, map<op_type, shared_ptr<TilingCases>>>` 两级索引。单例的取得方式有个测试钩子——[L68-L76](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L68-L76) 在 `ASCENDC_OP_TEST` 宏下只声明 `GetInstance()`（实现放到 UT 侧的 cpp，方便测试控制与观测），正常编译则用函数内 static 单例。不带 soc 的 `TilingRegistry` 是同样结构的降维版本，其 `DoTilingImpl` 见 [L245-L262](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L245-L262)，遍历逻辑与带 soc 版完全一致。

链式注册入口 `RegisterNew::tiling` 支持一次传多个 soc（[L202-L213](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L202-L213)），返回 `*this` 允许连续点号调用。

最后看四个注册宏，[L322-L347](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L322-L347)：

| 宏 | 索引维度 | 本仓库使用者 |
| --- | --- | --- |
| `REGISTER_TILING_TEMPLATE` | 算子名（字符串需带引号） | MHC 的 pre / pre_grad |
| `REGISTER_TILING_TEMPLATE_WITH_SOCVERSION` | 算子名 + 多个 soc + 优先级 | FA 前向/反向、PioneerBackward |
| `REGISTER_TILING_TEMPLATE_NEW` | 算子名 + 单个 soc + 优先级 | （当前无使用者） |
| `REGISTER_OPS_TILING_TEMPLATE` | 算子名（不带引号）+ 优先级 | MHC 的 sinkhorn/post 系、SparseFAGrad |

宏注释（L323、L329）点明优先级规则：**priority 越小优先级越高**。

两份真实落地对照：

- **sinkhorn（单实现）**：入口文件 [manifold_constrained_hyper_connection_sinkhorn_enhance_tiling_base.cpp:L23-L36](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling_base.cpp#L23-L36) 只有两件事：`TilingForSinkhorn` 把 context 递给**不带 soc** 的 `TilingRegistry`（L25），再用 `IMPL_OP_OPTILING(...).Tiling(TilingForSinkhorn).TilingParse<SinkhornCompileInfo>(...)` 挂到 CANN 框架（L34-L36）。实现文件末尾一行 [manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:L580](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L580) 注册唯一候选 `SinkhornTilingBase`，优先级 2000。「入口文件薄、实现文件注册」的两级拆分让入口稳定、实现可无限拆文件。
- **FlashAttention（六模板链）**：入口 [flash_attention_score_enhance_tiling.cpp:L287-L306](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L287-L306) 先做 `CheckParams` 与空输入特判（`IsEmptyInput` 直接填 tilingData 并 `SetTilingKey(1)`，L300-L302，绕过责任链），然后交给**带 soc** 的 `TilingRegistryNew`（L304）。六个模板按优先级 90/94/95/96/97/98 注册在 [arch32/flash_attention_score_enhance_tiling_general.cpp:L5054-L5089](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L5054-L5089)，覆盖 ASCEND910B 与 ASCEND910_93 两代芯片。

其中最精妙的是 90 号模板 `FlashAttentionScoreEnhanceTilingDropMask`：它的 `DoOpTiling` 在 [L4993-L5029](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L4993-L5029) 计算完 dropout 掩码的切分参数后**无条件返回 `GRAPH_PARAM_INVALID`**（L5028）——它是「贡献后放行」的链成员：只往共享 TilingData 里写 `dropmaskParams`，然后把主切分让给 94-98 号模板。这之所以可行，是因为 FA 基类的 `tilingData` 指针指向 **TilingContext 的原始 tiling data 缓冲**（[L584-L585](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L584-L585)，`context_->GetTilingData<...>()`），链上每个实例写的都是同一块内存。

而「真正让位」的范例是 95 号模板的 `IsCapable()`（[L3384-L3412](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3384-L3412)）：比对 S2 上限、数据量与 UB 预算，不匹配则 `OP_LOGE` 打印双方参数后 `return false` → 基类转成 `GRAPH_PARAM_INVALID` → 链继续。

#### 4.2.4 代码实践

**实践目标**：盘点全仓库的责任链注册现状，建立「算子 × 模板 × 优先级 × 是否带 soc」的全景矩阵。

**操作步骤**：

1. 在仓库 `training/` 目录执行：

   ```bash
   grep -rn "REGISTER_TILING_TEMPLATE\|REGISTER_OPS_TILING_TEMPLATE" \
        ascendc/src/ops-transformer --include="*.cpp" | grep -v "define"
   ```

2. 对每条命中，追进文件看：注册的类名、优先级数字、soc 列表（`WITH_SOCVERSION` 版本第四个参数）。
3. 按「算子家族」分组统计注册条数，并与 4.2.3 的两个样板对照。

**需要观察的现象**：MHC 家族普遍是「单类 + 优先级 2000 + 不带 soc」；Attention 家族是「多模板 + 优先级 1~98 + 带 soc」。

**预期结果**（笔者在当前 HEAD 已执行，共 26 处注册、另有 4 处宏定义位于框架头文件）：FA 前向 6 处（90/94/95/96/97/98，带 soc）、FA 反向 10 处（`flash_attention_score_grad_enhance` 各 arch32 切分实现，带 soc）、`AiInfraAttentionPioneerBackward` 2 处（arch35，带 soc）、`SparseFlashAttentionGradEnhance` 1 处（优先级 1，不带 soc）、MHC 家族 7 处（sinkhorn/sinkhorn_grad/mhc_post_grad 确认优先级均为 2000；pre/pre_grad/post/post_grad 的注册调用跨多行，优先级数值待确认）。读者运行结果应与此一致；若未来代码演进，以自己的 grep 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：如果两个 tiling 类用**同一个优先级**注册到同一个算子，会发生什么？

答案：`TilingCases::AddTiling`（L45-L47）检测到 key 已存在，打一条 `OP_LOGE("There are duplicate registrations.")` 后直接 return——先注册者生效，后来者被忽略，进程不会崩溃也不会断言。这是一个容易静默踩坑的点：新增模板前应先查该算子已占用的优先级。

**练习 2**：`DoTilingImpl` 遍历中，某个模板 `DoOpTiling` 返回了 `GRAPH_FAILED`（例如 shape 校验失败），链会继续尝试下一个模板吗？

答案：不会。L122-L125 的判断是 `status != GRAPH_PARAM_INVALID` 即返回——`GRAPH_FAILED` 同样终止整链。只有 `GRAPH_PARAM_INVALID` 才表示「我不支持，请找下家」；参数校验失败属于真错误，换一个模板也救不回来。

**练习 3**：为什么 `TilingRegistryNew::DoTilingImpl` 里 platformInfo 为 null 时敢直接 `static_cast<const CompileInfoCommon*>(context->GetCompileInfo())`？这条路径什么时候走？

答案：这是与 UT 框架的约定接缝：u8 将讲到的 `TilingContext faker` 伪造的 context 没有 `fe::PlatFormInfos`，但会把平台快照塞进 `TilingParse` 阶段注册的 CompileInfo 结构（`TilingPrepareForSinkhorn` / `TilingPrepareForFlashAttentionScoreEnhance` 在编译期填充）。`CompileInfoCommon` 的字段布局（socVersion 在 L55）是两边共同遵守的二进制契约，字段顺序不能随意改。真实硬件路径则走 L108-L116 的 `PlatformAscendC::GetSocVersion()`。

### 4.3 tiling_key.h：tilingKey 的十进制位编码

#### 4.3.1 概念说明

u2-l3/l4 已建立概念：tilingKey 是 **Host 写、Device 读的分支信号**——kernel 入口用 `TILING_KEY_IS(key, N)` 选择实例化哪个模板。单实现算子（aggregate_hidden 用 0/1 区分 bf16/fp16，sinkhorn 用 0/1 区分推理/训练路径）手写小整数即可；但当分支维度增多（布局 × 数据类型 × 稀疏模式 × 切分轴……），手写数字会失控。`tiling_key.h` 提供**十进制按位组装**方案：每个维度占一个十进制数位，维度的枚举值就是该位上的数字。

编码规则的（以 FA 家族为例的）官方注释在头文件里写得很清楚，大意是：从低位到高位依次是 Ub0、Ub1（UB 核内切分轴）、Block（分核轴）、DataType、Format/Layout、Sparse，各占一个十进制位；其余特化场景可以定义自己的位域。

#### 4.3.2 核心流程

`RecursiveSum` 把变参列表组装成十进制数——第一个参数落在个位：

\[ \text{RecursiveSum}(a_0, a_1, \ldots, a_{n-1}) = \sum_{i=0}^{n-1} a_i \cdot 10^{i} \]

`GET_TILINGKEY` 再加一个 \( 10^{19} \) 的偏移：

\[ \text{tilingKey} = 10^{19} + \sum_{i=0}^{n-1} a_i \cdot 10^{i} \]

例如 `GET_TILINGKEY(1, 2, 3)` 展开为 \( 10^{19} + 1 + 2\times10 + 3\times100 = 10^{19} + 321 \)。

三个设计要点：

1. **每个参数必须 ≤ 9**：十进制位组装没有进位保护，参数 ≥ 10 会「渗」到高一位，污染相邻维度的编码。
2. **\( 10^{19} \) 偏移是命名空间**：`uint64_t` 最大约 \( 1.8 \times 10^{19} \)，\( 10^{19} \) 起头既能放下又远离手写小 key（0、1、2……）的取值空间——运行期看到 key ≥ \( 10^{19} \) 就知道是框架编码生成的。
3. **与编译产物的关系**：tilingKey 的每一种取值对应 kernel 的一个特化分支（`TILING_KEY_IS` 命中的一个），op_build 会为各分支生成/选择对应的二进制；Host 侧 `GetTilingKey()` 返回什么值，Device 侧就必须有同值的分支接住——两侧常量同值是跨侧契约（u2-l4 已总结）。

#### 4.3.3 源码精读

`RecursiveSum` 的递归实现（含终止重载）在 [ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h:L24-L33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h#L24-L33)：空参返回 0 作递归基，一般形式 `templateId + kBase * RecursiveSum(rest...)` 用 `constexpr` 完成编译期计算——整个 key 在编译期就拼好了，零运行时开销。

编码规则注释（各数位含义）见 [L35-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h#L35-L47)，其中 L46-L47 给出了使用示例：`GET_TILINGKEY(AxisEnum::AXIS_S1, AxisEnum::AXIS_S2, AxisEnum::AXIS_N2, SupportedDtype::FLOAT32, InputLayout::BSH, SparseCapability::SUPPORT_ALL)`。

\( 10^{19} \) 偏移常量与入口模板在 [L49-L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h#L49-L53)；便捷宏 `TILINGKEY(ub2, ub1, block, dtype, layout, sparse)` 在 [L58-L60](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h#L58-L60)，直接接受六个枚举名。

真实消费侧的对照：sinkhorn 走「手写小 key」路线——`GetTilingKey()` 按 `outFlag` 返回 0 或 1（[manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:L556-L561](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L556-L561)），注释写明 0=训练/Transpose 路径、1=推理/DataCopyPad 路径。FA 则走「位编码」路线——各模板的 `GetTilingKey()` 返回 `GET_TPL_TILING_KEY(...)`（如 [arch32/flash_attention_score_enhance_tiling_general.cpp:L5047-L5050](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L5047-L5050)，20 个参数各占一位）。注意 `GET_TPL_TILING_KEY` 本身由 CANN 侧 `fase_tiling` 组件提供（FA 入口文件 L29 `using namespace fase_tiling;`），本仓库内无其定义——它是 `GET_TILINGKEY` 同思路的扩展版。另外 FA 入口的空输入特判直接 `SetTilingKey(1)`（[flash_attention_score_enhance_tiling.cpp:L274](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L274)），与 `FA_EMPTY_TILING_KEY` 常量（L43）对应。

#### 4.3.4 代码实践

**实践目标**：不依赖运行环境，手工推演编码公式的正确性，并对比两种 key 风格的适用边界。

**操作步骤**：

1. 抄写 `RecursiveSum` 的两条重载（L24-L33），在纸上展开 `RecursiveSum(7, 0, 3, 1)`，逐层写出递归栈。
2. 计算 `GET_TILINGKEY(7, 0, 3, 1)` 的完整值（含偏移）。
3. 阅读注释 L36-L44，回答：为什么 Ub0/Ub1/Block 要用「轴枚举」而不是布尔开关？允许最多切分两根轴意味着什么？
4. 反向练习：看到 key = \( 10^{19} + 50401 \)，写出各位数字对应的维度值序列（从个位到高位）。

**需要观察的现象**：递归展开时第一个参数乘 \( 10^0 \)、最后一个参数乘的 10 的幂次最高；步骤 4 的答案是 (1, 0, 4, 0, 5)（个位到万位）。

**预期结果**：`RecursiveSum(7, 0, 3, 1) = 7 + 0×10 + 3×100 + 1×1000 = 1307`；`GET_TILINGKEY(7, 0, 3, 1) = 10^19 + 1307 = 10000000000000001307`。此为纯编译期数学，可用任意 C++ 编译器写 5 行 `static_assert` 验证（待本地验证：在容器内用 bisheng 或 g++ 编译包含该头文件的测试单元）。

#### 4.3.5 小练习与答案

**练习 1**：为什么选十进制位组装，而不是 C 结构体的二进制位域（bitfield）？

答案：十进制方案下每个维度的合法值一目了然（key 的第 i 位就是第 i 个维度），日志里打印 key 即可人工解码，排查问题快；实现只需 `constexpr` 乘加，不依赖编译器位域布局。二进制位域虽然紧凑，但可读性差、且跨 Host/Device 序列化要操心对齐。本质是用「空间换可读性」——uint64 有 19 个十进制位可用，对分支维度数量绰绰有余。

**练习 2**：某天有人给 `TILINGKEY` 宏的 `dtype` 位传了一个值为 12 的枚举，会发生什么？

答案：12 占两个十进制位，`12 × 10^3 = 12000` 会同时挤占 layout 位（本来 ×10^4）——编码被污染，且因为组装是合法算术，编译期不会报错，只在运行期表现为 kernel 选错分支。这类 bug 极难排查，所以每个维度枚举必须保证值域 0~9（这也是注释中各枚举都设计为个位数的原因）。

**练习 3**：sinkhorn 为什么不用 `GET_TILINGKEY` 而手写 0/1？

答案：它只有两个分支（推理/训练路径），手写小 key 更直观，kernel 侧 `TILING_KEY_IS` 匹配也简单。位编码的价值在于维度组合爆炸的场景——「简单场景手写、复杂场景编码」是本仓库的惯例取舍。

### 4.4 公共小工具：tiling_util 与 data_copy_transpose_tiling

#### 4.4.1 概念说明

`tiling_base` 目录里还有两个不起眼但值得读的小件：

- **`tiling_util`**（头 `tiling_util.h` + 实现 `tiling_util.cpp`）：三个工具——`IsRegbaseSocVersion` 两个重载与 `EnsureNotScalar`。前者是「当前芯片是否 regbase 新架构」的判断（当前版本恒为 false，属预留开关，与 u4-l8 将讲的 arch35/regbase 算子族相关）；后者把标量 shape（维度数为 0）安全地当作 `{1}` 处理，避免 tiling 代码对空维度除零或越界。
- **`data_copy_transpose_tiling`**：FA 家族做布局转换（如 ND 排布转置）时，kernel 侧转置搬运所需的形状参数打包器。它把「目标形状/源形状的各维及若干预乘积」一次性填进 `CopyTransposeTiling` 结构，kernel 拿到后免于现场做乘法。

#### 4.4.2 核心流程

`GetDataCopyTransposeTiling` 的输入输出：

```text
输入: dstShape（转置目标形状, 四维 B/N/S/H）、srcShape（源形状）、typeSize（元素字节数）
输出: optiling::CopyTransposeTiling（写入以下字段）
    dstShapeB/N/S/H     目标四维
    dstShapeHN          = dstShapeH / dstShapeN   （每个 head 的 D 维大小）
    srcShapeB/N/S/HN    源四维
    originalShapeNLen   = srcShapeHN * typeSize
    shapeSHValue / shapeNsValue / shapeNsnValue / shapeBHValue  各维乘积的预计算
```

`EnsureNotScalar` 的逻辑一句话：`shape.IsScalar()` 时返回静态的 `{1}` 形状引用，否则原样返回——用「共享只读对象」避免按值拷贝 `gert::Shape`。

#### 4.4.3 源码精读

`tiling_util.cpp` 全文只有 30 余行，见 [ascendc/src/ops-transformer/common/src/tiling_base/tiling_util.cpp:L22-L49](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_base/tiling_util.cpp#L22-L49)：

- L22 定义函数级静态 `g_vec_1_shape = {1}`——`EnsureNotScalar`（L43-L49）返回的就是它的引用，因此**返回引用是安全的，但调用方不得修改**。
- L24-L27 的 `IsRegbaseSocVersion(SocVersion)` 无条件 `return false`：两个 context 重载（L29-L41）分别从 `TilingParseContext` / `TilingContext` 取 platformInfo 再调它。当前版本的结论是「所有芯片都走非 regbase 路径」；等 regbase 架构全面铺开后，只需改这一个函数。

`GetDataCopyTransposeTiling` 在 [ascendc/src/ops-transformer/common/include/tiling_base/data_copy_transpose_tiling.h:L25-L50](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/data_copy_transpose_tiling.h#L25-L50)：L28-L31 定义四个维度下标常量（B/N/S/H 分别是第 0/1/2/3 维），L35-L49 依次填目标形状、`dstShapeHN = H/N`（L39）、源形状与预乘积。结合 FA 的 ND 排布转换场景（输入 [B,S,N*D] 转成 [B,N,S,D]，此时四维表示里的 H=N*D，故 H/N 即 D）可以理解这些字段的几何含义——这是 FA 入口文件 include 它的原因（[flash_attention_score_enhance_tiling.cpp:L21](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L21)）。具体的 kernel 侧消费逻辑在 u4-l3 展开，本讲只需认识「Host 侧参数打包器」这一定位。

#### 4.4.4 代码实践

**实践目标**：确认两个工具的真实使用面，理解「公共件被谁依赖」。

**操作步骤**：

1. 执行下面两条检索，统计引用面：

   ```bash
   grep -rln "tiling_base/tiling_util.h" ascendc/src/ops-transformer --include="*.cpp" --include="*.h"
   grep -rln "data_copy_transpose_tiling" ascendc/src/ops-transformer --include="*.cpp" --include="*.h"
   ```

2. 打开 `IsRegbaseSocVersion` 的调用点（若有），观察调用方在 true/false 两条分支上分别做什么。
3. 阅读第 2 步未覆盖的场景，回答：如果把 `EnsureNotScalar` 的返回类型从 `const gert::Shape&` 改成按值 `gert::Shape`，功能还正确吗？有什么代价？

**需要观察的现象**：引用面远小于 `tiling_base.h` / `tiling_templates_registry.h`（后者几乎被所有框架化算子的 op_host include）——公共件也分「地基」和「边角料」两级。

**预期结果**：`data_copy_transpose_tiling` 主要被 FA 前向/反向的 op_host 引用；`tiling_util` 引用面较窄。第 3 步答案：功能正确（拷贝一份 `{1}` 同样安全），代价是每次调用多一次 Shape 对象的堆分配/拷贝，而 tiling 在图编译期可能被高频调用。实际运行检索命令的具体命中清单待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`IsRegbaseSocVersion` 恒返回 false，为什么还要保留两个 context 重载？

答案：为调用方提供类型适配（`TilingContext` 与 `TilingParseContext` 是不同生命周期阶段的上下文），并把「取 platformInfo → 构造 PlatformAscendC → 取 socVersion」的样板代码封装掉。等真要区分 regbase 时，改动被锁死在实现函数一处，所有调用方无感。

**练习 2**：`GetDataCopyTransposeTiling` 里为什么把 `shapeSHValue`、`shapeBHValue` 这类乘积在 Host 侧预计算好？

答案：kernel 侧每一次乘法都发生在 AIV 上，转置搬运的地址计算是热路径；把不变的乘积前置到 Host tiling 阶段算一次，通过 TilingData 传下去，设备侧只做加法与移位。这是 tiling「能算的都在 Host 算」通用原则的具体体现（与 u2-l3 的 TilingData 三组字段同理）。

**练习 3**：`EnsureNotScalar` 返回的静态 `{1}` 形状如果被某个调用方意外修改了，会发生什么？

答案：`g_vec_1_shape` 是所有调用方共享的函数级 static，一处修改会污染后续所有走到标量分支的 tiling 请求，且症状随机出现、极难定位。返回 `const&` 只挡住了通过该引用写入的常见路径，是「约定优先」的设计；更严格的写法是按值返回（见 4.4.4 第 3 步的取舍讨论）。

## 5. 综合实践

本讲的综合实践把两个真实算子串起来：**为 sinkhorn 画出一帧请求的 tiling 类执行链，并说明 FlashAttention 与该框架的对应关系**。全程只需读代码与画图，无需 NPU。

**实践目标**：

1. 用一张执行链图说清「CANN 调度 → 入口函数 → 注册表 → 责任链 → 三态返回」的完整路径，特别是 `GRAPH_PARAM_INVALID` 的回退路径。
2. 用一张对应关系表说清 FA 的 tiling_common.h / 自有基类 / 六个模板分别对应框架的哪一层。

**操作步骤**：

1. **画 sinkhorn 执行链**。按以下顺序阅读并画图（每一步都标上文件与行号）：
   - 入口注册：[manifold_constrained_hyper_connection_sinkhorn_enhance_tiling_base.cpp:L34-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling_base.cpp#L34-L36) 的 `IMPL_OP_OPTILING` 把 `TilingForSinkhorn` 挂到算子 `ManifoldConstrainedHyperConnectionSinkhornEnhance`。
   - 入口转发：L23-L26 的 `TilingForSinkhorn` 只有一行实质代码——调 `TilingRegistry::GetInstance().DoTilingImpl(context)`（不带 soc 的注册表）。
   - 链遍历：对照 [tiling_templates_registry.h:L245-L262](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L245-L262)，画出「按 priority 升序 → 工厂构造 → DoTiling() → 判三态」的循环框。
   - 唯一候选：[manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:L580](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L580) 注册的 `SinkhornTilingBase`（priority=2000）。画出其 `DoTiling()` 内部七步（基类 [tiling_base.h:L81-L113](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L81-L113)）。
   - **回退路径**：在图上用虚线画出 `GRAPH_PARAM_INVALID` 分支——`IsCapable()==false`（sinkhorn 恒 true，此路不通）或钩子显式返回 PARAM_INVALID 时，遍历下一个 priority；本例没有下一个候选，于是走到 [tiling_templates_registry.h:L260-L261](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L260-L261) 的失败出口。结论：sinkhorn 链长为 1，PARAM_INVALID 等价于失败。
   - 成功出口：`PostTiling()` 写 blockDim/TilingData，基类 L110 统一 `SetTilingKey(GetTilingKey())`（0 或 1）。

   参考骨架（请补全行号后誊入自己的笔记）：

   ```text
   CANN 框架（按 op_type 查到 IMPL_OP_OPTILING 注册项）
      │
      ▼
   TilingForSinkhorn(ctx)                        _tiling_base.cpp:L23-L26
      │
      ▼
   TilingRegistry::DoTilingImpl(ctx)             tiling_templates_registry.h:L245-L262
      │  op_type = ctx->GetNodeType()
      │  候选表 = registry_map_[op_type]  （std::map<priority, 工厂>，升序）
      ▼
   priority=2000 ──► new SinkhornTilingBase ──► DoTiling()      tiling.cpp:L79 / tiling_base.h:L81
        │  GetShapeAttrsInfo   (L157)  ──FAILED──► 整链中止
        │  GetPlatformInfo     (L143)
        │  IsCapable           (L95)   ──false──► GRAPH_PARAM_INVALID ──┐
        │  DoOpTiling          (L491)                                  │ 无下一候选
        │  DoLibApiTiling      (L534)                                  ▼
        │  GetWorkspaceSize    (L539)                        OP_LOGE + GRAPH_FAILED
        │  PostTiling          (L546)                                  （L260-L261）
        │  SetTilingKey(0|1)   (基类 L110)
        ▼
   GRAPH_SUCCESS
   ```

2. **写 FA 对应关系说明**。逐行填写下表（「框架侧」列已给出，FA 侧请自行补行号并核对）：

   | 框架侧（common/tiling_base） | FlashAttention 侧 | 说明 |
   | --- | --- | --- |
   | `CompileInfoCommon`（tiling_base.h:L45-L57） | `FlashAttentionScoreEnhanceCompileInfo`（flash_attention_score_enhance_tiling_common.h:L24-L32） | 算子私有的平台快照结构体；tiling_common.h 这个文件**只放了这个结构体**，它不是「框架本体」，而是框架 CompileInfo 约定的算子侧实现 |
   | `TilingBase` 七钩子 | `FlashAttentionScoreEnhanceTilingBase`（general.cpp:L329 起，继承 `TilingBase`） | FA 在公共基类之上又叠一层 FA 专用基类，抽走 layout/sparse 解析等公共逻辑，再派生六个特化模板 |
   | `TilingRegistryNew::DoTilingImpl` | 入口 `TilingFlashAttentionScoreEnhance` 末尾的调用（tiling.cpp:L304） | FA 用**带 soc** 的注册表，模板按芯片（910B/910_93）区分 |
   | priority 升序遍历 | 90 DropMask → 94 VarLen → 95 SameAB → 96 S1s2Bn2gs1 → 97 S1Bn2gs1 → 98 B（general.cpp:L5054-L5089） | 六级责任链的真实顺序 |
   | `GRAPH_PARAM_INVALID`（IsCapable=false） | 95 号模板 `IsCapable`（general.cpp:L3384-L3412） | 模板不匹配时的标准让位路径 |
   | `GRAPH_PARAM_INVALID`（钩子显式返回） | DropMask 的 `DoOpTiling`（general.cpp:L4993-L5029，填完 dropmaskParams 后返回） | 「贡献后放行」变体：借 PARAM_INVALID 继续链，副作用写入共享 tilingData（L584-L585） |
   | `Reset(context)`（tiling_base.h:L116-L119） | FA 基类 L337-L341 的 override + 私有 `Reset()` | 成员状态复位约定 |

3. **（可选，需环境）跑一次 UT 观察链日志**。若有容器环境，按 u8 将讲的方式编译 op_host UT（`bash build.sh -u -n <算子> -c ascend910_93 --ophost`），在用例中把日志级别调到 DEBUG，观察 `Do general op tiling success priority=%d` / `Ignore general op tiling priority=%d`（registry.h:L254-L257）两条日志——它们就是责任链遍历的运行时脚印。此步待本地验证。

**预期结果**：两张图/表完成后，你应能不假思索回答三个问题——(1) sinkhorn 的链长为什么是 1，它的 `_tiling_base.cpp` 与 `_tiling.cpp` 各自为什么存在；(2) FA 的 tiling_common.h 与框架是什么关系（CompileInfo 契约的算子侧实现，而非框架本体）；(3) `GRAPH_PARAM_INVALID` 有哪两种产生方式（IsCapable 让位 / 钩子显式返回），两者在 FA 链上分别由谁示范。

## 6. 本讲小结

- `TilingBase::DoTiling()` 是模板方法：固定「shape 属性 → 平台 → 能力判断 → 数据切分 → 高阶 API → workspace → 落盘 → 统一 SetTilingKey」七步流程，子类填七个纯虚钩子；执行顺序保证了 `IsCapable` 可以使用前两步填好的信息。
- 三态返回值是调度协议：`GRAPH_SUCCESS` 结束整链、`GRAPH_FAILED` 中止整链并上抛、`GRAPH_PARAM_INVALID` 让位给下一优先级——责任链的全部魔法就在这一个枚举值上。
- `tiling_templates_registry` 用「（socVersion →）op_type → priority → 工厂函数指针」的多级 map 组织候选实现，static 全局对象在 main 之前完成注册；同优先级先到先得，后来者被静默忽略。
- 两套注册表对应两种需求：不带 soc 的 `TilingRegistry`（MHC 系单实现）与带 soc 的 `TilingRegistryNew`（FA/Pioneer 多芯片多模板）；后者的 socVersion 探测有 platformInfo 与 CompileInfo 双通道，后者正是 UT 无硬件回放的接缝。
- `tiling_key.h` 用十进制位组装编码 tilingKey（首个参数落个位，加 \( 10^{19} \) 偏移与手写小 key 隔离），要求每维枚举 ≤ 9；FA 的 20 参数 `GET_TPL_TILING_KEY` 是同思路的 CANN 侧扩展。
- FA 链上的 DropMask 模板展示了 PARAM_INVALID 的高级用法——「贡献后放行」：只写共享 tilingData 的一节，然后把主切分让给后续模板；共享的载体是 `context_->GetTilingData<>()` 返回的同一块缓冲。

## 7. 下一步学习建议

- **u3-l4（stub 桩机制）**：本讲多次出现 `ASCENDC_OP_TEST` 宏与 CompileInfo 双通道——stub 讲将解释 UT 如何伪造 `TilingContext`、`fe::PlatFormInfos` 与 level0 算子符号，把本讲的「UT 接缝」补完整。
- **u4-l2 / u4-l3（FA 前向 tiling 与 kernel）**：本讲只画了 FA 责任链的骨架；u4 将进入 95/96 号模板内部，看 B/N2/G/S1/S2 的具体切分算法与 tilingKey 位域如何映射到 kernel 的布局模板。
- **u8-l1 / u8-l2（UT 框架与 Tiling 单测）**：动手给 tiling 写用例时，你会直接操作本讲的注册表（UT 侧 `GetInstance()` 的外部定义）与 `TilingContextPara`，届时回看 4.2 的调度流程会有豁然开朗之感。
- **延伸阅读**：对照 [flash_attention_score_grad_enhance 的 arch32 目录](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/arch32/flash_attention_score_grad_enhance_tiling_s1s2_bn2gs1s2.cpp)——它的 10 处注册是本仓库最长的一条 tiling 责任链，适合作为本讲内容的自测材料：能否不看讲义说出每个 priority 失败后的下一个候选是谁？
