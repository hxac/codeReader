# 多芯片与多架构适配：ascend910b / 910_93 / 950

## 1. 本讲目标

本讲是高级主题单元的第一篇。前面九个单元里，我们反复看到 `AddConfig("ascend910b", ...)`、`SocVersion::ASCEND910_93`、`arch32/`、`arch35/` 这些与芯片相关的代码，但一直没有系统性地回答：**一个算子要"跑在一款芯片上"，到底要过几道门？**

学完本讲，你应该能够：

1. 说清楚**编译期适配**（`_def.cpp` 的 `AICore().AddConfig`）与**运行期适配**（tiling 里的 `socVersion` 白名单校验）这两道门的职责边界与先后关系。
2. 理解为什么编译期注册了不代表运行期一定能跑——用 sparse FA 的真实反例说明"窄门"现象。
3. 掌握仓库用 `arch32/`（对应 ascend910b/910_93 这一代表面）与 `arch35/`（对应 ascend950）目录隔离两代平台实现的工程手法，以及 CMake 如何把两套实现挂进同一个 `optiling` 目标。
4. 了解更细粒度的运行时探测：`GetCurNpuArch()` / `NpuArch::DAV_3510`，以及 tiling 模板"按 soc 注册"的机制。
5. 独立完成两件事：整理全仓库的**芯片支持矩阵**；为 `ai_infra_aggregate_hidden` **列出新增 ascend950 支持的完整改动清单**。

## 2. 前置知识

本讲默认你已读过 u2-l2（`_def.cpp` 与 OpDef 注册）、u2-l3（Tiling 入门）和 u4-l8（AttentionPioneer）。先用三段话把需要的背景重新激活：

**芯片命名速查。** 本仓库涉及三款芯片型号字符串：`ascend910b`（Atlas A2 训练系列）、`ascend910_93`（Atlas A3 训练/推理系列）、`ascend950`（Ascend 950PR/DT）。它们与 README 提供的三类 docker 镜像一一对应——A2/A3 镜像带 `cann8.5.0`，A5 镜像带 `cann9.0.0`。**注意一个易混点**：`build.sh -c` 的参数值是芯片型号字符串，而 A2/A3/A5 是产品代际俗称，README 中"以 A3 环境举例"对应的参数是 `-c ascend910_93`。

**一个重要纠偏（承接 u4-l8）。** 大纲早期把 `arch35` 目录描述为"A3 类芯片"，这是**不准确的**。源码证据非常明确：`ai_infra_attention_pioneer_def.cpp` 只为 `arch35/` 实现注册了 `AddConfig("ascend950", ...)`，且 `arch35` 内的 tiling 代码在运行时判断 `NpuArch::DAV_3510`（Davinci 3.5 架构，即 950）。所以正确的对应关系是：

| 目录名 | 服务芯片 | 典型算子 | 运行时架构标识 |
| --- | --- | --- | --- |
| `arch32/` | ascend910b、ascend910_93 | flash_attention_score_enhance（前向/反向） | 同一实现双 soc 注册 |
| `arch35/` | ascend950 | ai_infra_attention_pioneer（前向/反向/metadata） | `NpuArch::DAV_3510` |

**两道门的时间线。** 编译期（`bash build.sh -c ascend910_93`）决定"哪些算子会被编进这个芯片的 run 包"；运行期（框架下发一次算子调用，触发 tiling）决定"这次调用在当前硬件上是否被放行"。两道门都过了，kernel 才会被启动。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp` | 编译期门标本：`AICore().AddConfig("ascend910b"/"ascend910_93", ...)` |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp` | 运行期门标本：`GetNpuInfo()` 中的 socVersion 白名单 + 按核数微调切分 |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_def.cpp` | 950 侧编译期门标本：`aicore_config_95` 与 `AddConfig("ascend950", ...)` |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_tiling.cpp` | 950 侧 tiling 薄入口：转发到 `arch35` 的 v2 实现 |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp` | arch35 平台探测：`GetCurNpuArch()`、`GetSocVersion()` |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_backward/op_host/arch35/ai_infra_attention_pioneer_backward_tiling_normal_regbase.cpp` | 运行时架构探测的容错写法（compileInfo 优先于 GetCurNpuArch） |
| `ascendc/src/ops-transformer/attention/common/op_host/fia_tiling_templates_registry.h` | FA 家族"按 soc 注册 tiling 模板"的责任链注册表 |
| `ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h` | 通用 tiling 模板注册宏（含 soc_versions 变参版本） |
| `ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp` | "窄门"反例：def 注册双芯片、tiling 只放行 910B |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/config.ini` | AICPU 算子的芯片门禁声明 |
| `ascendc/build.sh` | `-c` 参数解析与向 CMake 的透传 |
| `ascendc/README.md` | 镜像选择、编译命令、产品支持表的文档依据 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：双层适配模型、编译期门、运行期门、目录级隔离、更细粒度的架构探测。

### 4.1 双层适配模型：一条算子调用要过几道芯片门

#### 4.1.1 概念说明

"适配一款芯片"不是一句话，而是三层各自独立的承诺：

1. **编译层（AddConfig）**：`_def.cpp` 声明"本算子为芯片 X 准备了一份 AICore 配置"。这决定了 `op_build` 工具在打包时会不会为芯片 X 生成 kernel 二进制与调度信息。没注册，就**编不进去**。
2. **运行层（socVersion 校验）**：tiling 函数在 Host 上被调用时，从 `TilingContext` 读出当前硬件的真实 `socVersion`，与白名单比对。不在名单内，直接 `return ge::GRAPH_FAILED`。注册了，也**不一定放行**。
3. **实现层（arch 目录 / NpuArch 分支 / 核数微调）**：同一款芯片家族内部还有微差异（AIV 核数不同、指令集代际不同），代码用 `arch32/`/`arch35/` 目录、`GetCurNpuArch()` 判断和 `aivNum_` 特判来吸收。

#### 4.1.2 核心流程

以一次 `bash build.sh -c ascend910_93` 加一次算子调用为例，芯片相关的判定按时间顺序是：

```text
编译期：
  build.sh -c ascend910_93
    → -DASCEND_COMPUTE_UNIT=ascend910_93 传给 CMake
    → CMake glob 收集算子目录（-n 白名单过滤）
    → op_build 读每个 _def.cpp 的 AICore().AddConfig(...)
    → 只为 ascend910_93 生成产物 → 封装 run 包

运行期（每次算子调用）：
  框架按算子名找到已安装的 optiling 实现
    → tiling 函数被调用
    → GetPlatformInfo() → PlatformAscendC → GetSocVersion()
    → socVersion 与白名单比对（失败即 GRAPH_FAILED，调用终止）
    → GetCoreNumAiv() / GetCoreNumAic() 取真实核数
    → （可选）GetCurNpuArch() 探测架构代际，走 arch32/arch35 分支
    → 切分、SetTilingKey、SetBlockDim → kernel 启动
```

一个关键推论：**AddConfig 是必要条件，不是充分条件**。编译期能过、运行期被 tiling 拒绝，是完全可能的（见 4.3 的 sparse FA 反例）。

#### 4.1.3 源码精读

先看编译期与运行期两道门在 `ai_infra_aggregate_hidden` 上的最小样本。

编译期，`_def.cpp` 末尾两行就是第一道门的全部：

[ai_infra_aggregate_hidden_def.cpp:83-84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L83-L84)——把同一份 `aicore_config` 分别注册给 `ascend910b` 与 `ascend910_93`，即"一份实现服务两款芯片"：

```cpp
this->AICore().AddConfig("ascend910b", aicore_config);
this->AICore().AddConfig("ascend910_93", aicore_config);
```

运行期，tiling 侧的白名单在 `GetNpuInfo()` 里：

[ai_infra_aggregate_hidden_tiling.cpp:88-93](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L88-L93)——从 `TilingContext` 拿平台信息，读出真实 `socVersion`，与 `ASCEND910B`、`ASCEND910_93` 两个枚举比对，不匹配则打日志并失败返回：

```cpp
socVersion_ = ascendcPlatform.GetSocVersion();
if ((socVersion_ != platform_ascendc::SocVersion::ASCEND910B) &&
    (socVersion_ != platform_ascendc::SocVersion::ASCEND910_93)) {
    OP_LOGE(opName_, "SOC Version[%d] is not support.", (int32_t)socVersion_);
    return ge::GRAPH_FAILED;
}
```

注意这两处的**命名对齐**：字符串 `"ascend910_93"`（def）与枚举 `SocVersion::ASCEND910_93`（tiling）是同一款芯片在两层的两种写法，靠人肉保持一致——这正是本讲实践任务要你用 grep 系统性核对的原因。

#### 4.1.4 代码实践

**实践目标**：亲手验证"编译期注册 ≠ 运行期放行"。

**操作步骤**（纯源码阅读型，无需 NPU）：

1. 在 `ascendc/src/ops-transformer` 下执行 `grep -rn 'AddConfig("' --include='*_def.cpp' | grep -v ascend910b`，找出所有**不含** 910b 注册的 def 文件。
2. 对第 1 步找到的每个算子，打开其 tiling 实现，搜索 `GetSocVersion`，确认运行期白名单与 def 注册是否一致。
3. 记录任何"def 注册集合 ⊃ tiling 白名单集合"的算子。

**需要观察的现象**：你应该会发现 `sparse_flash_attention_enhance` 是典型样本——def 同时注册 910b 与 910_93，tiling 却只接受 910B（详见 4.3.3）。

**预期结果**：得到一份"编译期/运行期芯片集合差异表"。绝大多数算子两层一致，少数存在窄门。

**待本地验证**：grep 命令本身可直接运行；若要观察运行期拒绝现象，需要 910_93 真机上调 sparse FA 并看 `SOC Version is not support` 日志，本环境无 NPU，无法执行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `_def.cpp` 里的 `AddConfig("ascend910_93", aicore_config)` 删掉，但保留 tiling 白名单里的 `ASCEND910_93`，会发生什么？

**答案**：编译 `ascend910_93` 的 run 包时，该算子不会为 910_93 生成产物（第一道门就关了）；即使 tiling 白名单还留着 910_93，运行时框架根本找不到可用的 910_93 实现或 aclnn 符号，调用失败。tiling 的白名单此时形同虚设——它只在"编译产物存在"的前提下才有意义。

**练习 2**：为什么 tiling 里校验 socVersion 要放在 `CoreSplit()`（切分计算）之前？

**答案**：因为切分依赖平台参数（`aivNum_` 等）。若不先确认芯片型号就做切分，`GetCoreNumAiv()` 拿到的核数可能不属于已验证的芯片，切分结果（blockDim、baseH 等）对 kernel 来说就是未经校验的垃圾输入。先过门、再取数、再切分，是防御式编程的顺序要求（可对照 `ParseAndCheck` 中 `GetNpuInfo` 先于 `CoreSplit` 的调用顺序，见 [ai_infra_aggregate_hidden_tiling.cpp:398-411](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L398-L411)）。

---

### 4.2 编译期门：AddConfig 与全仓库芯片支持矩阵

#### 4.2.1 概念说明

`OpAICoreConfig` 是"一份针对特定芯片的算子编译配置"。`AddConfig(soc, config)` 把它挂到算子原型上，语义是：**为芯片 soc 生成这份配置描述的 kernel**。同一算子可以注册多份不同配置（pioneer 为 950 单独准备了 `aicore_config_95`，dtype 集合比 910b 版本更宽，含 FP8 类型），也可以像 aggregate_hidden 一样一份配置注册两次。

这道门决定了 run 包里的产物**份数**：`-c ascend910_93` 编出的包不含 950 产物，反之亦然。所以"全仓库芯片支持矩阵"的第一手依据就是各 `_def.cpp` 的 AddConfig 集合。

#### 4.2.2 核心流程

- 一份 config 包含：输入输出的类型/格式约束（芯片相关的 dtype 集合）、六个能力开关（`DynamicCompileStaticFlag` 等）、`ExtendCfgInfo` 键值对（如 `jitCompile.flag`、`coreType.value`）。
- `AddConfig` 可以多次调用，每次一个 soc 字符串；也可以为不同 soc 传**不同的 config 对象**，实现"同算子不同芯片不同 dtype 支持"。
- `OP_ADD(类名)` 把整个 OpDef（含全部 AddConfig）登记进注册表，`op_build` 据此生成 aclnn 接口与编译产物。

#### 4.2.3 源码精读

**样本一：一份配置服务两款芯片**。aggregate_hidden 在构造好 `aicore_config` 后连注册两次：

[ai_infra_aggregate_hidden_def.cpp:48-54](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L48-L54)——`OpAICoreConfig` 的构造与第一项 Input 声明，这个 config 对象随后被两个 soc 共用：

```cpp
OpAICoreConfig aicore_config;
aicore_config.Input("input")
    .ParamType(REQUIRED)
    .DataType({ge::DT_BF16, ge::DT_FLOAT16})
    ...
```

**样本二：不同芯片不同配置**。pioneer 为 950 准备了独立的 `aicore_config_95`，其 Output 的 dtype 集合明显更宽（含 `DT_HIFLOAT8` 等 910 系列没有的类型），并在最后注册：

[ai_infra_attention_pioneer_def.cpp:2494-2507](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_def.cpp#L2494-L2507)——`softmax_lse` 输出声明、能力开关与 950 注册：

```cpp
aicore_config_95.Output("softmax_lse")
    .ParamType(REQUIRED)
    .DataTypeList({ge::DT_FLOAT})
    .FormatList({ge::FORMAT_ND});
aicore_config_95.DynamicCompileStaticFlag(true)
    ...
    .ExtendCfgInfo("opFile.value", "ai_infra_attention_pioneer")
    .ExtendCfgInfo("jitCompile.flag", "static_false,dynamic_false");
this->AICore().AddConfig("ascend950", aicore_config_95);
```

值得注意 `ExtendCfgInfo("opFile.value", "ai_infra_attention_pioneer")`：它把 950 产物归属到指定 opFile 名下，是编译工具链按芯片组织产物文件的钩子——**同一个类名，950 版本可以有自己的文件归属与 jitCompile 策略**。

**样本三：全仓库矩阵的原始数据**。对 18 个算子的 def/config.ini 做 grep，得到（详见第 5 节综合实践的完整矩阵）：

- 16 个算子：`ascend910b` + `ascend910_93` 双注册（MoME 全部、MHC 全部、FA 前向/反向、稀疏 FA 前向/反向、LightningIndexer、KL Loss 反向）。
- 2 个算子：仅 `ascend950`（ai_infra_attention_pioneer 前向与反向，`ai_infra_attention_pioneer_def.cpp:2507` 与 `ai_infra_attention_pioneer_backward_def.cpp:678`）。
- 1 个特殊：`ai_infra_attention_pioneer_metadata` 是 AICPU 形态，无 `_def.cpp` 的 AddConfig，改用 `config.ini` 声明（见 4.5.3）。

#### 4.2.4 代码实践

**实践目标**：用文档交叉验证编译期注册，体会"文档可能滞后、源码是唯一事实"。

**操作步骤**：

1. 打开 [ai_infra_aggregate_hidden/README.md:3-12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L3-L12) 的产品支持情况表。
2. 对照 `_def.cpp:83-84` 的 AddConfig 集合。
3. 写下三行结论：文档说支持谁、源码说支持谁、两者是否一致。

**需要观察的现象**：README 表格中 `Atlas A3 训练系列 √`、`Atlas A2 训练系列 √`、`Ascend 950PR/DT ×`，与 def 中 `ascend910b + ascend910_93` 双注册完全吻合。

**预期结果**：aggregate_hidden 这个算子的文档与源码一致。但注意这只是抽样——u2-l1 已示范过 README 存在笔误的案例（"output 是 input 的梯度"），所以矩阵整理必须以 def 源码为准、文档做旁证。

#### 4.2.5 小练习与答案

**练习 1**：pioneer 为什么不把 910b/910_93 也注册上，而是 950 独占？

**答案**：因为它的实现在 `arch35/` 目录，是围绕 Davinci 3.5（950）的架构特性写的（如 `GetCurNpuArch() != NpuArch::DAV_3510` 的分支、`FAKernelNoquantMla` 的混核流水）。把这份实现注册到 910b 意味着要在 910 的指令集上编译 arch35 代码，大概率编不过或性能极差。芯片注册必须与实现所在目录的能力匹配。

**练习 2**：`AddConfig` 传同一个 config 对象两次（aggregate_hidden 的写法）与传两个不同 config（pioneer 的写法），各自适用什么场景？

**答案**：同一份 config 复用，适用于两款芯片上 dtype/格式/能力开关完全一致的实现——910b 与 910_93 同属 arch32 表面，向量/Cube 指令兼容，tiling 只需按核数微调。不同 config 则用于芯片能力有实质差异时，比如 950 支持 FP8 系列 dtype 而 910 不支持，此时 dtype 集合、`ExtendCfgInfo` 都需要分开声明。

---

### 4.3 运行期门：tiling 的 socVersion 白名单与"窄门"反例

#### 4.3.1 概念说明

运行期门写在 tiling 里，本质是一句"当前硬件是不是我验证过的型号"。它存在的理由有三：

1. **防御**：AddConfig 只管编译，包可能被装到任何机器上；tiling 是最后一道关卡。
2. **精确性**：同一 soc 家族内的核数、UB 大小不同，切分逻辑要先确认环境再取数。
3. **能力收窄**：某些实现只在一款芯片上调优/验证过（如稀疏 FA 只在 910B 上放行），用窄门显式表达"未验证即不支持"。

#### 4.3.2 核心流程

```text
GetPlatformInfo() → PlatformAscendC 包装
  → GetCoreNumAiv() / GetCoreNumAic()   （核数，0 即失败）
  → GetSocVersion()                      （芯片型号枚举）
  → 白名单比对 → 不在名单 → OP_LOGE + GRAPH_FAILED
  → 后续 CoreSplit 使用 aivNum_ 做切分
```

#### 4.3.3 源码精读

**反例标本：sparse FA 的窄门。** 前向 def 注册了双芯片（`sparse_flash_attention_enhance_def.cpp:96-97`），但 tiling 只放行 910B：

[sparse_flash_attention_enhance_tiling.cpp:1543-1547](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp#L1543-L1547)——白名单只有 `ASCEND910B` 一个成员：

```cpp
socVersion_ = ascendcPlatform.GetSocVersion();
if (socVersion_ != platform_ascendc::SocVersion::ASCEND910B) {
    OPS_REPORT_VECTOR_INNER_ERR(opName_, "SOC Version[%d] is not support.", static_cast<int32_t>(socVersion_));
    return GRAPH_FAILED;
}
```

这意味着：在 910_93 机器上，该算子**能编译、能安装、能被框架找到**，但每次调用都在 tiling 阶段失败。这是"编译期注册集合 ⊃ 运行期放行集合"的活样本——也提醒我们读代码时不能只看 def 就下结论。

**家族内微差异的吸收：核数特判。** 910b 与 910_93 都过同一道白名单，但 AIV 核数可能不同（48 核与 40 核两档）。aggregate_hidden 的 `CoreSplit()` 用显式特判吸收：

[ai_infra_aggregate_hidden_tiling.cpp:296-312](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L296-L312)——按 `aivNum_` 是 48 还是 40 调整 `baseHCnt_`，让切分份数能整除核数：

```cpp
// aivNum_ == 48 | baseHCnt_ == 5
if (aivNum_ == FOURTY_EIGHT && baseHCnt_ == NUM_FIVE) {
    baseHCnt_ = NUM_SIX;
    flagChange = NUM_ONE;
}
// aivNum_ == 40 | baseHCnt_ == 6
if (aivNum_ == FOURTY && baseHCnt_ == NUM_SIX) {
    baseHCnt_ = NUM_EIGHT;
    flagChange = NUM_ONE;
}
// aivNum_ == 40 | baseHCnt_ == 3
if (aivNum_ == FOURTY && baseHCnt_ == NUM_THREE) {
    baseHCnt_ = NUM_FOUR;
    flagChange = NUM_ONE;
}
```

这里的洞察是：**socVersion 白名单解决"能不能跑"，核数特判解决"跑得好不好"**。两款芯片过同一道门后，差异下沉到平台参数（核数/UB）驱动的普通数值逻辑里——这是"一套实现适配一个家族"的标准姿势。若新增一款核数不同的芯片（如 950），这些硬编码特判就是必须逐一审查的高危点。

#### 4.3.4 代码实践

**实践目标**：统计运行期门在本仓库的分布，识别窄门算子。

**操作步骤**：

1. 执行 `grep -rn "GetSocVersion()" --include='*_tiling*.cpp' ascendc/src/ops-transformer | grep -v tests`，列出所有在 tiling 中读取 socVersion 的位置。
2. 对每个命中文件，读上下文 5 行，记录白名单成员集合。
3. 与 4.2 得到的 AddConfig 集合做差集。

**需要观察的现象**：多数算子的白名单为 `{ASCEND910B, ASCEND910_93}`，与 def 一致；`sparse_flash_attention_enhance` 的白名单为 `{ASCEND910B}`，是差集非空的样本；FA 前向的 tiling 主体（`flash_attention_score_enhance_tiling.cpp`）甚至不直接做 soc 白名单，而是把 soc 集合下放到每个 tiling 模板的注册参数里（见 4.5）。

**预期结果**：得到"算子 × 运行期白名单"表，能明确指出哪些算子在 910_93 上编译可用但调用会被拒。

**待本地验证**：在 910_93 真机上调用 sparse FA 观察报错日志这一步无 NPU 环境，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：sparse FA 为什么 def 要注册 910_93、tiling 却拒绝？这样写有什么好处和坏处？

**答案**：合理推测是：def 注册让包结构与家族保持一致（编译、aclnn 生成、依赖管理统一），而实现只在 910B 上完成验证，窄门是诚实表达"未验证"的方式。好处是失败信息明确（有 SOC Version 日志）、后续放开只需删一行判断；坏处是使用者若只看 def 或文档会误以为支持 910_93，直到运行才失败——所以支持矩阵必须以 tiling 为准做第二遍核对。

**练习 2**：`CoreSplit()` 里 `aivNum_ == 48` / `== 40` 的特判，换成 `socVersion_ == ASCEND910B` 之类的判断会不会更好？

**答案**：不会。核数才是切分逻辑真正依赖的变量（切分要整除核数），直接判核数让代码对"同 soc 不同核数配置"的变体也稳健；而判 socVersion 会引入一层不必要的间接（soc → 核数映射），且新增芯片时要改更多分支。这也体现了适配的一个分层原则：**门禁看 soc，算法看平台参数**。

---

### 4.4 目录级隔离：arch32 / arch35 双实现并存

#### 4.4.1 概念说明

当两款芯片的指令集代际差异大到"一套代码适配不动"时，仓库用**目录隔离**：把平台专属实现放进 `op_host/arch32/`、`op_host/arch35/`、`op_kernel/arch32/`、`op_kernel/arch35/` 子目录，通用入口留在上层。当前仓库的目录分布（`ls` 可验证）：

- `arch32/`：flash_attention_score_enhance 与 flash_attention_score_grad_enhance 的 op_host、op_kernel。
- `arch35/`：ai_infra_attention_pioneer、ai_infra_attention_pioneer_backward 的 op_host、op_kernel；ai_infra_attention_pioneer_metadata 的 op_host；attention/common 的 op_kernel。

注意：**目录隔离是"实现摆放"问题，不是"芯片识别"机制**。识别仍靠 4.1–4.3 的两层门与 4.5 的运行时探测；arch 目录只是让两代实现互不污染，由 CMake 统一挂进编译目标。

#### 4.4.2 核心流程

以 pioneer（950 专属）为例，调用链是：

```text
框架 → IMPL_OP_OPTILING(AiInfraAttentionPioneer).Tiling(DoOpTilingAiInfraAttentionPioneer)
     → ai_infra_attention_pioneer_tiling.cpp: DoOpTilingAiInfraAttentionPioneer（薄入口）
     → arch35/ai_infra_attention_pioneer_tiling_v2.cpp: TilingAiInfraAttentionPioneerV2（真实现）
     → 平台探测 → 切分 → 写 CANN 标准 TilingData 下传
```

而 FA（arch32 家族）的入口 `flash_attention_score_enhance_tiling.cpp` 则把切分下放给 `arch32/flash_attention_score_enhance_tiling_general.cpp` 里的多个模板类。

#### 4.4.3 源码精读

**薄入口转发**。pioneer 的通用层 tiling 文件只有一个转发函数：

[ai_infra_attention_pioneer_tiling.cpp:27-33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_tiling.cpp#L27-L33)——入口函数直接调用 arch35 的 v2 实现：

```cpp
AP_EXTERN_C ge::graphStatus DoOpTilingAiInfraAttentionPioneer(gert::TilingContext *context)
{
    OP_CHECK_IF(context == nullptr,
        OPS_REPORT_VECTOR_INNER_ERR("AiInfraAttentionPioneer", "Tiling context is null."),
        return ge::GRAPH_FAILED);
    return TilingAiInfraAttentionPioneerV2(context);
}
```

同文件 35-41 行还有一个 `extern "C"` 的 `DeviceDoOpTilingAiInfraAttentionPioneer` 导出——这是为 tiling 下沉（u9-l2 将讲的 tiling_sink）预留的设备侧同款入口，同一份实现可被 Host 侧或设备侧调用。

**注册与实现的分离**。IMPL_OP_OPTILING 不在实现文件里，而在独立注册文件中：

[ai_infra_attention_pioneer_tiling_register.cpp:26-29](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_tiling_register.cpp#L26-L29)——把入口函数挂到框架，并注册 `TilingParse` 钩子（编译期缓存平台信息的入口，见 u3-l3）：

```cpp
IMPL_OP_OPTILING(AiInfraAttentionPioneer)
    .Tiling(DoOpTilingAiInfraAttentionPioneer)
    .TilingParse<AiInfraAttentionPioneerCompileInfo>(
        TilingPrepareForAiInfraAttentionPioneer);
```

**CMake 挂接：arch 目录与通用文件进同一个目标**。pioneer 的 op_host CMakeLists 把三件东西（通用 tiling、注册文件、arch35 实现）编进同一个 `optiling` 库：

[ai_infra_attention_pioneer/op_host/CMakeLists.txt:24-34](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/CMakeLists.txt#L24-L34)：

```cmake
target_sources(optiling PRIVATE
        ai_infra_attention_pioneer_tiling.cpp
        ai_infra_attention_pioneer_tiling_register.cpp
        arch35/ai_infra_attention_pioneer_tiling_v2.cpp
)
```

FA 侧同理，[flash_attention_score_enhance/op_host/CMakeLists.txt:25-28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/CMakeLists.txt#L25-L28) 把 `arch32/flash_attention_score_enhance_tiling_general.cpp` 挂进 `optiling`。

**关键结论**：CMake 不按 `-c` 的 soc 值筛选 arch 目录——arch32 与 arch35 的 Host 侧 tiling 代码都是普通 C++，可以编进任何目标；真正的芯片差异在**设备侧 kernel 编译**（由 op_build 按 AddConfig 生成对应指令集二进制）和**运行时探测**中体现。这也是为什么"新增芯片"时 Host 侧改动往往比 kernel 侧轻。

#### 4.4.4 代码实践

**实践目标**：核实 arch 目录的真实分布，纠正"大纲认知"。

**操作步骤**：

1. 执行 `ls -d ascendc/src/ops-transformer/*/*/op_host/arch* ascendc/src/ops-transformer/*/*/op_kernel/arch*`。
2. 对每个命中目录，看它属于哪个算子，再打开该算子 `_def.cpp` 的 AddConfig 行，记录"arch 目录 ↔ 注册芯片"的对应。
3. 对照 4.1 的表格，写下你的结论。

**需要观察的现象**：`arch32` 只出现在 FA 前向/反向（双 910 注册），`arch35` 只出现在 pioneer 家族（950 注册）+ attention/common。

**预期结果**：得到与 4.1 表格一致的结论——**arch32 ↔ ascend910b/910_93（A2/A3 表面，Davinci 3.2 时代），arch35 ↔ ascend950（Davinci 3.5）**。大纲中"arch35（A3 类）"的说法应据此修正。

#### 4.4.5 小练习与答案

**练习 1**：既然 pioneer 是 950 独占，为什么不把 `arch35/ai_infra_attention_pioneer_tiling_v2.cpp` 的内容直接放进上层 `ai_infra_attention_pioneer_tiling.cpp`，省掉一层目录？

**答案**：目录隔离是为**演化和共存**预留的：一旦将来要为 910b/910_93 补一份 pioneer 实现（或为 950 写新一代实现），只需平行新增 `arch32/` 目录并在入口做分发，不必在一坨文件里用 `#ifdef` 或 if-else 混流。FA 家族正是这个形态的实证——入口在上层，多个 arch32 模板在下层按 tilingKey/soc 责任链选择。

**练习 2**：`ai_infra_attention_pioneer_metadata` 的 op_host 也有 `arch35/`，但它是 AICPU 算子（无 AICore kernel），这个目录里放的是什么？

**答案**：放的是 Host 侧 infershape 等 AICPU 算子仍需要的宿主实现（AICPU 算子形态是 op_graph 原型 + op_kernel_aicpu 的 CPU 实现 + config.ini，op_host 仅承担 infershape，无 tiling——见 u4-l9）。`arch35` 在这里标记的同样是"这份宿主逻辑服务 950"。

---

### 4.5 更细的粒度：NpuArch 运行时探测、按 soc 注册的 tiling 模板与 config.ini 门禁

#### 4.5.1 概念说明

socVersion 枚举之外，平台还提供 `GetCurNpuArch()` 返回 `NpuArch` 枚举（如 `DAV_3510` 表示 Davinci 3.5 架构）。它是比 soc 型号更接近"指令集代际"的标识，主要用于：

- 在**同代多型号**间共享实现（都是 DAV_3510 的芯片走同一分支）。
- 在 x86 仿真/UT 环境下做**容错探测**（`GetCurNpuArch` 可能返回无效值，需要 fallback）。

另外两个本模块要覆盖的点：FA 家族的 tiling 模板注册表**按 soc 维度建桶**（注册时声明支持哪些 soc，运行时按当前 soc 取桶）；AICPU 算子用 `config.ini` 声明芯片门禁。

#### 4.5.2 核心流程

FA 家族的模板责任链（与 u3-l3 的 tiling_base 责任链同构，但多了 soc 维度）：

```text
注册期（静态初始化）：
  REGISTER_TILING_TEMPLATE_WITH_SOCVERSION(算子名, 模板类, {910B, 910_93}, 优先级)
    → registry_map_[soc][op_type] 按优先级插入

运行期：
  DoTilingImpl(context)
    → GetCurNpuArch() 得 npuArch
    → GetTilingTemplates(opType, npuArch) 取该架构的模板桶
    → 按优先级逐个尝试，GRAPH_PARAM_INVALID 则让位下一个
```

#### 4.5.3 源码精读

**arch35 运行时探测**。pioneer 的 tiling v2 在校验对齐要求时直接用架构判断：

[ai_infra_attention_pioneer_tiling_v2.cpp:451-459](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L451-L459)——只有非 DAV_3510 架构才强制 int8 的 32 对齐检查，950 上放宽：

```cpp
if (ascendcPlatform.GetCurNpuArch() != NpuArch::DAV_3510) {
    OP_CHECK_IF((((contextParamsForPFATiling.inputDataType == ge::DT_INT8) ||
                  ... (queryD % D_ALIGN_32 != 0)),
                ... "D(%u) of query should be 32 elements aligned when int8 is involved!", ...);
}
```

同文件的平台探测块也是 950 实现的"取数标准件"：[ai_infra_attention_pioneer_tiling_v2.cpp:400-414](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L400-L414)——一次性取齐 AIV/AIC 核数、UB/L1/L0 各级内存大小与 `socShortName`（`GetSocVersion()` 的返回），供后续切分与 `RunBigKernelTilingWithParams` 使用。

**x86 容错探测（pioneer backward 的三层 fallback）**。UT/仿真环境下 `GetCurNpuArch()` 可能拿不到真实值，反向算子的处理方式值得抄录：

[ai_infra_attention_pioneer_backward_tiling_normal_regbase.cpp:143-156](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_backward/op_host/arch35/ai_infra_attention_pioneer_backward_tiling_normal_regbase.cpp#L143-L156)——优先信 `TilingParse` 缓存的 `compileInfo->socVersion`，仅在拿不到时用 `GetCurNpuArch()`，并对无效值（`0xFFFF`）再做一次 compileInfo 兜底：

```cpp
// Prioritize socVersion from compileInfo to avoid GetCurNpuArch error on x86
if (compileInfoPtr != nullptr && compileInfoPtr->socVersion == platform_ascendc::SocVersion::ASCEND950) {
    npuArch = NpuArch::DAV_3510;
} else {
    npuArch = ascendcPlatform.GetCurNpuArch();
    // Fallback: if GetCurNpuArch returns invalid on x86, try compileInfo again
    if (npuArch == static_cast<NpuArch>(0xFFFF) && compileInfoPtr != nullptr) {
        if (compileInfoPtr->socVersion == platform_ascendc::SocVersion::ASCEND950) {
            npuArch = NpuArch::DAV_3510;
        }
    }
}
```

这里 `socVersion → npuArch` 的手工映射（950 → DAV_3510）正是 soc 与 arch 两个标识体系的**桥接点**——新增芯片时这类映射散点都要排查。

**按 soc 注册的模板责任链（FA 家族）**。注册表以 soc 为第一维 key：

[fia_tiling_templates_registry.h:96-127](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/common/op_host/fia_tiling_templates_registry.h#L96-L127)——`DoTilingImpl` 用 `GetCurNpuArch()` 选桶、按优先级试模板，`GRAPH_PARAM_INVALID` 让位下一个（u3-l3 三态语义在 soc 维度上的复刻）：

```cpp
npuArch = static_cast<int32_t>(ascendcPlatform.GetCurNpuArch());
...
auto tilingTemplateRegistryMap = GetTilingTemplates(opType, npuArch);
for (auto it = tilingTemplateRegistryMap.begin(); it != tilingTemplateRegistryMap.end(); ++it) {
    auto tilingTemplate = it->second(context);
    if (tilingTemplate != nullptr) {
        ge::graphStatus status = tilingTemplate->DoTiling(tilingInfo);
        if (status != ge::GRAPH_PARAM_INVALID) {
            return status;
        }
    }
}
```

注册侧的宏调用带 soc 向量，[flash_attention_score_enhance_tiling_general.cpp:5054-5059](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L5054-L5059)——DropMask 模板以优先级 90 注册到 910B 与 910_93 两个桶（同文件 5060-5089 还有 94/95/96/97/98 五个模板同样注册）：

```cpp
REGISTER_TILING_TEMPLATE_WITH_SOCVERSION(
    FlashAttentionScoreEnhance,
    FlashAttentionScoreEnhanceTilingDropMask,
    std::vector<int32_t>({static_cast<int32_t>(platform_ascendc::SocVersion::ASCEND910B),
                          static_cast<int32_t>(platform_ascendc::SocVersion::ASCEND910_93)}),
    90);
```

通用版宏定义在 [tiling_templates_registry.h:324-339](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L324-L339)（`REGISTER_TILING_TEMPLATE_WITH_SOCVERSION` / `_NEW` 两个带 soc 的变体，注释明确"priority 越小优先级越高"）。

**AICPU 门禁：config.ini**。metadata 算子没有 AddConfig，芯片声明走 ini：

[config.ini:9-12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/config.ini#L9-L12)——文件头注释说明它"仅用于门禁工程识别算子适配芯片版本，不影响算子业务执行逻辑"：

```ini
[operater]
op_name = ai_infra_attention_pioneer_metadata
aicore_versions = ascend950
```

#### 4.5.4 代码实践

**实践目标**：把"芯片识别"的五种写法各找一处真实样本，形成速查卡。

**操作步骤**：

1. 在仓库内分别 grep 以下五类标识，各记录一处文件与行号：
   - `AddConfig("`（def 编译期注册）
   - `GetSocVersion()` + 白名单比对（tiling 运行期门）
   - `GetCurNpuArch()`（架构代际探测）
   - `REGISTER_TILING_TEMPLATE_WITH_SOCVERSION` 或 `_FIA`（按 soc 注册模板）
   - `aicore_versions`（AICPU config.ini 门禁）
2. 把五类写法按"编译期 / 运行期"分两组，各画一张小卡片：写法 → 文件位置 → 判定对象（能否编译 / 能否运行 / 选哪个实现）。

**需要观察的现象**：五类写法全部有真实命中，没有任何一类是空集；其中 `GetCurNpuArch` 只出现在 pioneer 家族与 fia 注册表，说明它是"新一代（950）与模板化 FA"专用的细粒度手段。

**预期结果**：一张五行的"芯片识别速查卡"，作为后续二开时的 checklist 底稿。

#### 4.5.5 小练习与答案

**练习 1**：`SocVersion::ASCEND950` 与 `NpuArch::DAV_3510` 是什么关系？为什么代码里两处都要写？

**答案**：socVersion 标识具体型号（产品维度），NpuArch 标识指令集架构代际（微架构维度）；950 属于 Davinci 3.5。两处都写是因为不同 API 返回不同维度：`GetSocVersion()` 在多数环境稳定可用，`GetCurNpuArch()` 直接对应"该跑哪套 arch 实现"，但在 x86 仿真下可能返回无效值——所以 backward 的代码做了 socVersion→DAV_3510 的手工映射兜底。新增芯片时，如果它属于新架构代际，这类映射点都要补。

**练习 2**：`config.ini` 里 `aicore_versions = ascend950` 写的是 "aicore"，但 metadata 是 AICPU 算子，矛盾吗？

**答案**：不矛盾但要留意。该字段名沿用了门禁工程的通用字段名（芯片版本列表），文件头注释已声明此文件"不影响算子业务执行逻辑"，即它只是给 CI 门禁用的元数据，不是运行时判断。真正的运行约束在算子自身的 infershape/实现里。这是"配置字段名与实际语义脱节"的常见工程债，读配置时要靠注释与实际消费方确认含义。

---

### 4.6 工程入口：build.sh -c 与镜像选择

#### 4.6.1 概念说明

所有编译期适配的入口是 `build.sh -c <soc>`，它把芯片字符串透传给 CMake 的 `ASCEND_COMPUTE_UNIT`。镜像则决定编译工具链（不同 CANN 版本支持不同芯片）。这是 u1-l3/u1-l4 已建立认知在本讲视角下的收口：**编译目标是芯片维度的第一份契约**。

#### 4.6.2 核心流程

```text
bash build.sh -c ascend950
  → 参数解析：ascend_compute_unit="ascend950"
  → -DASCEND_COMPUTE_UNIT=ascend950 追加进 CUSTOM_OPTION
  → cmake 配置 → 按 def 的 AddConfig 为 950 生成产物
  → run 包（仅含 950 产物）
```

#### 4.6.3 源码精读

参数帮助与解析：

[build.sh:55-56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L55-L56)——帮助文本声明三个合法值与默认值 `ascend910_93`：

```bash
echo "-c|--compute-unit    Specifies the chip type. ... The default is ascend910_93."
echo "                     For example: -c \"ascend910_93\" or -c \"ascend910b\""
```

[build.sh:271-274](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L271-L274) 与 [build.sh:357-359](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L357-L359)——`-c` 读入变量，再转成 CMake 定义：

```bash
-c|--compute-unit)
    ascend_compute_unit="$2"
    shift 2
    ;;
...
if [ -n "${ascend_compute_unit}" ];then
    CUSTOM_OPTION="${CUSTOM_OPTION} -DASCEND_COMPUTE_UNIT=${ascend_compute_unit}"
fi
```

文档侧的配套说明与镜像对应：

[README.md:174-177](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L174-L177)——A2/A3 镜像 cann8.5.0、A5 镜像 cann9.0.0；

[README.md:223-239](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L223-L239)——`soc_version` 取值 `(ascend910b、ascend910_93、ascend950)`，并给出三组示例命令（含 `-c ascend910_93` 编全部、`-n` 白名单编指定算子）。

由此得到镜像 ↔ `-c` 值的实操对应：**A2 镜像编 `ascend910b`，A3 镜像编 `ascend910_93`，A5 镜像（cann9.0.0）编 `ascend950`**。用错镜像（如 A2 镜像编 950）会在工具链版本校验处失败，可用 `--disable-check-compatible` 跳过校验但产物可用性自负（README 249 行的警告）。

#### 4.6.4 代码实践

**实践目标**：验证 `-c` 与 `-n` 的组合语义，为综合实践做准备。

**操作步骤**：

1. 通读 [build.sh:261-352](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L261-L352) 的完整参数解析 while 循环，列出 `-c`、`-n`、`-u`、`--disable-check-compatible` 各自写入的变量。
2. 追踪 `ascend_compute_unit` 变量从读入到 `-D` 透传的完整路径（共两处引用）。
3. 回答：`-c "ascend910b;ascend910_93"` 一次编两款芯片，靠的是什么机制？

**需要观察的现象**：帮助文本说多值用分号加引号；透传时是**单个** CMake 变量 `ASCEND_COMPUTE_UNIT` 承载整个分号串，由 CMake/op_build 侧再拆分。

**预期结果**：理解"多芯片一次编译 = 一个 CMake 变量承载分号分隔列表，下游逐个拆解生成多份产物"；同时确认 `-n` 是目录名白名单（与 UT 的 `-n` 同语义，见 u8-l4）。

**待本地验证**：实际多芯片编译需 A5/A3 镜像环境，本环境无 docker 与 NPU，命令未执行，待本地验证。

#### 4.6.5 小练习与答案

**练习 1**：为什么 `-c` 的默认值是 `ascend910_93` 而不是空？

**答案**：CMake 侧需要一个确定的编译目标才能配置工具链与产物格式；空值会导致 op_build 不知道为哪款芯片生成二进制。默认指向 910_93（A3）说明它是当前主力训练芯片——这与 README"以 A3 环境举例"相互印证。

**练习 2**：编 950 时如果漏了 `-c ascend950`，会发生什么？

**答案**：按默认 `ascend910_93` 编译。pioneer 家族的 `AddConfig("ascend950", ...)` 不会为 910_93 生成产物，编出的包在 950 机器上找不到可用实现；更早一步，A5 镜像里用 910_93 目标还可能触发 CANN 版本兼容性校验失败。芯片目标是编译的第一契约，漏写属于"静默错配"，要靠 `output` 目录产物名与安装后 `vendors` 目录内容来核对。

---

## 5. 综合实践

综合实践分两部分，把本讲五个模块串起来。**全部为源码阅读与文档产出型任务，不修改任何源码**。

### 任务一：整理全仓库芯片支持矩阵

**目标**：产出一张"算子 × ascend910b / ascend910_93 / ascend950"矩阵，并标注数据来源与置信度。

**步骤**：

1. **第一遍（def 依据）**：执行
   ```bash
   grep -rn 'AddConfig("' ascendc/src/ops-transformer --include='*_def.cpp'
   ```
   对 17 个命中文件（16 个 910 家族 + 1 个 pioneer；backward 是第 18 个算子，其中 metadata 无 def）按算子记录芯片集合。
2. **第二遍（config.ini 依据）**：对 AICPU 算子补 `grep -rn 'aicore_versions' ascendc/src/ops-transformer --include='config.ini'`。
3. **第三遍（tiling 依据）**：执行
   ```bash
   grep -rn "GetSocVersion()" ascendc/src/ops-transformer --include='*_tiling*.cpp' | grep -v tests
   ```
   读取每个命中处的白名单，标记与 def 集合不一致的"窄门"算子。
4. **第四遍（docs 旁证）**：抽查 3 个算子的 README 产品支持表（如 aggregate_hidden、pioneer、sinkhorn），与矩阵比对，不一致处注明"文档与源码冲突，以源码为准"。

**参考答案（可直接核对的底稿）**：

| 算子 | 910b | 910_93 | 950 | 备注 |
| --- | --- | --- | --- | --- |
| ai_infra_aggregate_hidden / _grad | √ | √ | × | def:83-84；README 表格一致 |
| MHC 全部 7 个（sinkhorn/_grad、pre/_grad、post/_grad、mhc_post_grad） | √ | √ | × | 各 def 均 910b+910_93 |
| flash_attention_score_enhance / _grad_enhance | √ | √ | × | arch32 实现；模板按 {910B,910_93} 注册 |
| sparse_flash_attention_enhance | 编√ | 编√ | × | **窄门**：tiling 仅放行 910B（tiling.cpp:1544） |
| sparse_flash_attention_grad_enhance | √ | √ | × | |
| lightning_indexer_enhance | √ | √ | × | tiling.cpp:109-110 白名单双芯片 |
| sparse_lightning_indexer_grad_kl_loss_enhance | √ | √ | × | |
| ai_infra_attention_pioneer / _backward | × | × | √ | arch35 / DAV_3510 |
| ai_infra_attention_pioneer_metadata | × | × | √ | AICPU，config.ini 声明 |

（√ 表示 def 注册；"编√"表示 def 注册但运行期被窄门拦截。）

### 任务二：为 aggregate_hidden 新增 ascend950 支持的改动清单

**目标**：不写代码，产出一份"要动哪些文件、动哪里、为什么"的审查清单。这是新增芯片支持的通用方法论演练。

**清单（按四层 + 工程与测试组织）**：

1. **def 层**（`ai_infra_aggregate_hidden_def.cpp`）：
   - 仿照 [pioneer_def.cpp:2494-2507](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_def.cpp#L2494-L2507) 新建 `aicore_config_95`（先按原 dtype 集合 BF16/FP16 复制，950 特有能力后续再放开），并追加 `this->AICore().AddConfig("ascend950", aicore_config_95);`。
   - 决策点：是否需要 `ExtendCfgInfo("opFile.value", ...)` 与独立的 `jitCompile.flag`（pioneer 的 950 配置带了这两项，aggregate_hidden 当前配置没有）。
2. **tiling 层**（`ai_infra_aggregate_hidden_tiling.cpp`）：
   - `GetNpuInfo()` 白名单加 `SocVersion::ASCEND950`（88-93 行处加一个 `||` 分支）。
   - **高危点**：`CoreSplit()` 的核数特判（296-312 行）只覆盖 48/40 两档核数；需先在 950 上实测 `GetCoreNumAiv()` 的值，再决定是补特判还是重构为通用整除逻辑。`H_SIZE_FULL`（UB 全载上限 4096）也要按 950 的 UB 大小复核。
3. **kernel 层**（`op_kernel/ai_infra_aggregate_hidden.cpp/.h`）：
   - 该算子无 arch 目录、单实现。需评估 950 指令集下现有 Ascend C 代码（DataCopyPad/Cast/PipeBarrier 等）是否兼容；若不兼容或需利用 950 新特性，按 FA/pioneer 先例引入 `op_kernel/arch35/` 目录并在入口分发。
   - tilingKey（0=BF16/1=FP16）与芯片无关，可复用；入口的 `TILING_KEY_IS` 分支无需改。
4. **工程与文档**：
   - `build.sh -c ascend950` 已支持（README:223），无需改 build.sh；需在 **A5 镜像**（cann9.0.0）中执行编译。
   - 算子目录若有 `CMakeLists.txt` 挂接 arch 文件（本算子 op_host 无 arch 源，当前无需改；引入 arch35 后要仿照 pioneer CMakeLists:24-34 加 `target_sources`）。
   - 更新 README 产品支持表（把 `Ascend 950PR/DT` 行从 × 改 √）与 docs 中约束。
5. **测试层**：
   - UT：按 u8-l2 方法在 `tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp` 中用 `TilingContextPara` 的平台伪造参数补 socVersion=950 的用例（伪造核数需按 950 实际值）。
   - ST：在 950 真机跑 `tests/st/test_ai_infra_aggregate_hidden.py` 验证精度（MARE/MERE/RMSE 判定，见 u8-l3）。

**验收标准**：清单能回答"每一层为什么动/为什么不动"，特别是 CoreSplit 核数特判与 kernel 指令兼容性这两个高危点有明确的验证手段（真机核数实测 + 编译试跑）。

## 6. 本讲小结

- **双层门模型**：编译期 `AICore().AddConfig(soc, config)` 决定产物为谁生成，运行期 tiling 的 `GetSocVersion()` 白名单决定本次调用是否放行；AddConfig 是必要不充分条件，sparse FA 是"注册双芯片、只放行 910B"的窄门活样本。
- **家族内微差异**用平台参数吸收：910b/910_93 过同一道白名单后，差异体现为 `GetCoreNumAiv()` 的 48/40 两档特判——门禁看 soc，算法看平台参数。
- **目录隔离**：`arch32/` 服务 910b/910_93（FA 前反向），`arch35/` 服务 950（pioneer 家族）；Host 侧 arch 代码由 CMake 挂进同一 `optiling` 目标，不按 `-c` 筛选，芯片差异真正显形在设备侧 kernel 编译与运行时探测。**纠正**：arch35 是 Ascend 950（DAV_3510），不是大纲早期所说的 A3。
- **更细粒度**：`GetCurNpuArch()`/`NpuArch::DAV_3510` 标识指令集代际，x86 环境下需用 `TilingParse` 缓存的 `compileInfo->socVersion` 兜底（pioneer backward 的三层 fallback 是范本）；FA 家族的 tiling 模板用 `REGISTER_TILING_TEMPLATE_WITH_SOCVERSION` 按 soc 建桶注册。
- **AICPU 形态**的芯片声明不走 AddConfig，走 `config.ini` 的 `aicore_versions`（仅门禁识别用）。
- **工程入口**：`build.sh -c` → `-DASCEND_COMPUTE_UNIT` → CMake/op_build；A2/A3 镜像（cann8.5.0）对应 910b/910_93，A5 镜像（cann9.0.0）对应 950，镜像与 `-c` 必须配套。

## 7. 下一步学习建议

本讲把"芯片"这条横切线讲完了。第 9 单元还剩三讲，建议按序推进：

1. **u9-l2 tiling_sink**：本讲 4.4.3 提到 pioneer 入口文件里有 `DeviceDoOpTilingAiInfraAttentionPioneer` 这个设备侧导出——下一讲讲清楚 tiling 计算如何从 Host 下沉到设备端执行，那正是这个符号存在的理由。
2. **u9-l3 fallback 机制**：当输入不满足芯片/算子约束时，aclnn 层如何拆分组合算子完成任务，与本讲的"运行期拒绝"形成互补（拒绝 vs 降级）。
3. **u9-l4 综合实战**：把本讲的"新增芯片清单"方法论放大为"新增算子全流程"，建议先完成本讲任务二的清单再进入。
4. 延伸阅读：重读 u4-l8（pioneer 的 arch35 tiling_v2 全文）与 u8-l2（UT 伪造平台参数），本讲的 socVersion/arch 探测在这两处分别有最复杂与最简化的应用样本。
