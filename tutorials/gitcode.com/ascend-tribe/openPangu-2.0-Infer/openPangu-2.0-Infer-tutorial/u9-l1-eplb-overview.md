# OmniPlacement 专家负载均衡原理与启用

## 1. 本讲目标

本讲是 omni-eplb 单元的第一讲。学完后你应该能够：

1. 说清 MoE 推理中「专家负载不均」为什么发生、为什么在大规模 EP（专家并行）部署下会成为性能瓶颈。
2. 解释 OmniPlacement 的四板斧：专家重排列、层间不均匀部署、冗余专家、近实时激活采集与动态重排，以及它们分别解决什么问题。
3. 读懂部署三件套——wheel 包、`config.yaml`、placement pattern——各自的角色，特别是 pattern 文件那个三维 0/1 矩阵的语义。
4. 逐字段掌握 `config.yaml` 的配置项，并能在推理容器内完成构建与启用配置。
5. 说出关键约束条件：为什么冗余部署与 AllGather 通信模式不能同时开启、为什么 P 侧与 D 侧要分别配置。

本讲聚焦「原理 + 部署配置面」。OmniPlanner、ExpertMapping、placement_handler 等核心类的调用链精读留给下一讲 u9-l2。

## 2. 前置知识

### 2.1 回顾：MoE 路由与专家并行（来自 u3-l3）

- **路由（routing）**：MoE 层里有一个门控网络（gate），它给每个 token 打分，选出 top-k 个「路由专家」去处理这个 token，最后把各专家输出加权合并。openPangu-2.0 用 sigmoid 打分加偏置修正。
- **专家并行（EP）**：把 E 个路由专家切开分到多张卡（rank）上，每个 rank 只持有大约 E/EP 个专家的权重。token 需要跨卡寄送给持有目标专家的 rank。
- **expert_map**：vLLM 里描述「全局专家号 → 本地专家号」的映射，rank 上不存在的专家映射为负数。
- **逻辑专家与物理专家**：开启 EPLB（Expert Parallelism Load Balance）后，「逻辑专家」是模型视角的第 e 号专家，「物理专家」是实际部署在某张卡上的一个专家副本。一个逻辑专家可以有多个物理副本——这就是冗余。

如果这些概念已经模糊，建议先回看 u3-l3 的「MoE 路由与分发」与「专家并行」两节。

### 2.2 本讲新概念：负载不均与木桶效应

专家路由是模型学出来的，不是人为均匀分配的。真实业务流量下，某些「热门专家」会被远超平均比例的 token 选中。EP 组内每个 rank 的耗时取决于它持有的专家被命中的总工作量，而一次 MoE 前向的耗时由最慢的那个 rank 决定——这是典型的木桶效应：

\[ T_{\text{MoE}} = \max_{d \in \text{devices}} A[d], \quad A[d] = \sum_{l}\sum_{e} \text{act}[l][e] \cdot \mathbb{1}[\text{expert } (l,e) \text{ 部署在 } d] \]

其中 \(\text{act}[l][e]\) 是第 \(l\) 层第 \(e\) 号专家的激活量（被选中的 token 数 × 每 token 计算量）。负载均衡的目标就是通过改变「哪个专家放在哪张卡」这个部署矩阵 \(\mathbb{1}[\cdot]\)，去压低 \(\max_d A[d]\)。

这个问题本质上是带约束的装箱/划分问题（NP-hard），所以工程上用贪心与启发式优化器求解，而不是精确最优。

### 2.3 两个解决思路

1. **重排（rearrange）**：不增加任何显存开销，只是把专家位置重新洗牌——把热门专家挪到「闲卡」上。约束是每个逻辑专家恰好一个物理副本。
2. **冗余（redundant）**：给热门专家多做几个副本，分散到多张卡上共同承接流量。代价是显存（每张卡要装下更多专家），收益是负载更平 + 高可用（某个副本所在卡故障，其余副本还能服务）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `components/omni-eplb/README.md` | OmniPlacement SDK 的能力总述：四大特性、两大优化目标、冗余专家高可用 |
| `components/omni-eplb/Guideline.md` | 部署、启用、配置专家均衡的操作手册（功能目标与约束条件也在这里） |
| `components/omni-eplb/config.yaml` | 特性配置样例：pattern 路径、动态开关、冗余上限、dump 开关、优化器列表 |
| `components/omni-eplb/omni_placement/omni_planner.py` | OmniPlanner 单例入口，消费 config.yaml 的各字段（本讲只看 `__init__`，精读留给 u9-l2） |
| `components/omni-eplb/omni_placement/config.py` | 把 YAML 加载成对象属性的轻量 Config 类 |
| `components/omni-eplb/omni_placement/utils.py` | `_init_omni_eplb_configs` / `apply_omni_eplb_attributes`：omni-npu 启用链路的桥 |
| `components/omni-eplb/setup.py`、`build/build.sh` | wheel 构建与组件安装脚本 |
| `components/omni-npu/src/omni_npu/worker/npu_worker.py` | 引擎侧接入点 1：init_device 末尾初始化 eplb 配置 |
| `components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors_moe.py` | 引擎侧接入点 2：MoE 层构造 OmniPlanner 并扩展本地专家数 |
| `components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py` | 引擎侧接入点 3：接管 vLLM EplbState 的运行时补丁 |
| `components/omni-eplb/patterns/`、`patterns/base_patterns/` | 真实的 pattern 文件（.npy），含 505B decode 用 pattern 与 DSV3 基线 |
| `components/omni-eplb/utils/omni_pattern_tool/pattern_generation_pipeline.sh` | 静态均衡流水线：dump 数据 → 生成 pattern → 分析收益 |

一个先说清的重要事实：**本仓库 `tools/ansible` 的所有模板里没有任何 placement/eplb 相关配置**（用 grep 全目录检索无命中）。Guideline 中提到的 `omni_infer_server.yml`、`docker_update_prefill_code_cmd` 等属于上游内部仓 omni_infer 的部署体系。在本开源仓中启用 OmniPlacement 需要按 Guideline 的步骤手工操作，并把改动自行合入你的 ansible 模板。同时，开源仓中的启用开关也与 Guideline 描述的 `use_omni_placement: true` 不同——是 vLLM 标准的 `--enable-eplb` 加 `--additional-config`，证据见 4.4 节。

## 4. 核心概念与源码讲解

### 4.1 MoE 负载不均问题与 OmniPlacement 的解法

#### 4.1.1 概念说明

OmniPlacement 是一个面向 NPU 环境的 MoE 动态专家排布 SDK。README 把它的能力概括为四条：

> 1. Expert Rearrangement（专家重排列）：动态重配专家以优化资源利用。
> 2. Layer-Wise Uneven Expert Placement（层间不均匀部署）：支持专家在各层之间不均匀分布。
> 3. Near-Realtime Placement（近实时排布）：以可忽略的性能开销更新专家排布。
> 4. Near-Realtime Activation Capturing（近实时激活采集）：实时采集专家激活数据。

「层间不均匀」值得单独解释：不同 MoE 层的路由热度分布是不同的——第 3 层可能专家 17 最热，第 40 层可能专家 203 最热。如果所有层都用同一套「专家 e 固定放在 rank e mod R」的分布，就无法逐层适配。OmniPlacement 允许每一层有自己独立的部署矩阵，这是 pattern 文件带 layerid 维度的根本原因。

优化目标在 README 中明确为两条：**最小化最大激活负载**（对应 2.2 节的 \(\min \max_d A[d]\)，防止瓶颈卡）与**降低通信开销**（结合 NPU 集群拓扑优化排布，减少跨卡寄送）。

#### 4.1.2 核心流程

OmniPlacement 有两条工作模式（Guideline 3.1 节）：

**静态模式**（三步走）：

```
① dump 激活 ──► ② 离线生成 pattern 文件 ──► ③ 应用 pattern 并重启服务
   (enable_dump)     (omni_pattern_tool)         (pattern_path 指向 .npy)
```

**动态模式**（推荐）：服务运行期间近实时地采集激活、周期性计算新排布并热更新，不需要重启：

```
引擎每步 step()
   └─► 采集本步专家激活（近实时）
        └─► 周期触发优化器：基于累计激活计算新部署矩阵
             └─► 下发新 mapping 到各 rank（权重搬运对齐）
                  └─► 后续 step 按新排布路由
```

两条模式共享同一套激活统计基础设施——这正是「近实时激活采集」作为独立特性存在的原因：静态模式可以把它当 dump 工具用，动态模式把它当决策输入用。

#### 4.1.3 源码精读

四大特性与两大优化目标的原始表述：

- [components/omni-eplb/README.md:3-8](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/README.md#L3-L8)：SDK 定位与四条关键特性（重排、层间不均匀、近实时排布、近实时激活采集）。
- [components/omni-eplb/README.md:17-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/README.md#L17-L21)：两个优化目标——最小化最大激活负载、按集群拓扑降低通信开销。

Guideline 开篇对算法手段的中文概括与约束条件：

- [components/omni-eplb/Guideline.md:9-14](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L9-L14)：功能目标——专家部署排布优化、冗余部署、动态负载均衡策略、采集激活数据并生成静态部署 pattern。
- [components/omni-eplb/Guideline.md:16-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L16-L21)：约束条件四条，其中「冗余部署和 AllGather 通信模式不支持同时开启」是排障时最容易踩的坑（4.4 节展开）。

静态/动态两种模式的划分与推荐：

- [components/omni-eplb/Guideline.md:126-134](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L126-L134)：专家均衡支撑静态和动态两种模式；静态分 dump、生成 pattern、应用 pattern 三步；官方推荐动态模式。

冗余专家的第二重价值——高可用：

- [components/omni-eplb/README.md:27-28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/README.md#L27-L28)：在不中断服务的前提下动态部署冗余专家，显著降低大规模 MoE 系统的 RTO（故障恢复时间目标）。也就是说冗余副本平时分担负载，故障时顶替失效卡，一份数据两份收益。

#### 4.1.4 代码实践

**实践：用仓库自带的 pattern 文件验证「层间不均匀」**

1. 实践目标：直观看到 pattern 是逐层独立的部署矩阵，而不是一个全局统一分布。
2. 操作步骤（任意装了 numpy 的 Python 环境即可，无需 NPU）：

```bash
cd components/omni-eplb
python3 - <<'EOF'
import numpy as np
# 任选一个基线 pattern（8 die 版本最小）
p = np.load("patterns/base_patterns/DSV3_baseline_8_devices_58_MoE_Layers.npy")
print("shape =", p.shape)          # 期望 (devices, layers, epid) 三维
print("取值集合 =", np.unique(p))   # 期望只有 0 和 1
per_layer = p.sum(axis=2)          # 每层每卡部署的专家数
print("第 0 层各卡专家数:", per_layer[0])
print("第 30 层各卡专家数:", per_layer[30])
print("逐层分布是否完全相同:", np.all(per_layer == per_layer[0]))
EOF
```

3. 需要观察的现象：矩阵是三维 0/1 数组；基线 pattern 每层每卡专家数大概率相同（baseline 就是均匀铺），但换用 `patterns/` 下带 `rearrange`/`redundant` 字样的真实 pattern 后，逐层分布会出现差异。
4. 预期结果：`取值集合` 输出 `[0 1]`；对冗余 pattern，同一层全卡专家数之和会大于专家总数（存在副本）。具体数值**待本地验证**（本讲写作环境无法执行 numpy）。

#### 4.1.5 小练习与答案

**练习 1**：为什么「层间不均匀部署」能带来收益？统一分布损失了什么？

答案：不同 MoE 层的路由热度分布不同（各层 gate 是独立参数）。若强制所有层用同一分布，则每层都要在「同一个划分方案」下取最大值，相当于用一副药方治所有层；允许逐层独立排布后，每层都可以针对自己的热门专家做专门平衡，\(\max_d A[d]\) 的下界更低。

**练习 2**：重排（rearrange）与冗余（redundant）两种 pattern 的资源权衡是什么？Guideline 给出的按规模选择建议是什么？

答案：重排零显存额外开销（每个逻辑专家仍是一个物理副本），但均衡能力受「每卡专家数固定」约束；冗余要额外显存（卡上专家数增加），但能压平尖峰并提供故障冗余。Guideline 第 175 行建议：32 die 及以下用 rearrange pattern，32 die 以上用 redundant pattern（[components/omni-eplb/Guideline.md:171-176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L171-L176)）——规模越大，热门专家的绝对负载越高，只靠搬位置压不住，需要副本分摊。

### 4.2 部署三件套：wheel、config.yaml 与 placement pattern

#### 4.2.1 概念说明

Guideline 2.1 节说明 OmniPlacement 随 OmniInfer 交付，交付件固定是三样：

1. **omni_placement wheel 包**：可执行的 SDK 本体（Python + C++ 扩展）。
2. **Config.yaml**：特性配置文件，且「通常 prefill 节点和 Decode 节点分别配置」——即 `config_p.yaml` 与 `config_d.yaml` 两份。
3. **Placement pattern**：专家部署描述文件（.npy）。

pattern 为什么必须是独立文件？因为它是「离线分析/动态优化器」与「在线引擎」之间的数据接口：pattern 工具用 dump 的激活数据算出好排布，写成 .npy；引擎加载 .npy 恢复排布。两边解耦，排布可以独立迭代而不动引擎代码。

#### 4.2.2 核心流程

config.yaml 头部注释对 pattern 的语义给出了权威定义——一个三维二值矩阵 `expert_mapping`：

\[ \text{expert\_mapping}[\text{deviceid}][\text{layerid}][\text{epid}] \in \{0, 1\} \]

- `deviceid`：设备（die）编号；
- `layerid`：MoE 层编号；
- `epid`：层内专家编号。注意不同层的同号 `epid` 是不同专家。

取值 1 表示「第 layerid 层的专家 epid 部署在设备 deviceid 上」，0 表示不在。特别地，**同一个 (layerid, epid) 在多个 device 上可以同时为 1**——这正是冗余副本在数据结构上的表达；也意味着 (layerid, epid) 二元组不能唯一定位一次部署。

三件套的组装关系：

```
omni_placement wheel（装进容器）
        │
        ▼
config_p.yaml / config_d.yaml ──pattern_path──► pattern .npy（三维 0/1 矩阵）
        │                                            │
        └──────────── OmniPlanner 加载 ◄─────────────┘
```

#### 4.2.3 源码精读

- [components/omni-eplb/Guideline.md:24-28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L24-L28)：交付件三部分——wheel、Config.yaml（P/D 分别配置）、Placement pattern。
- [components/omni-eplb/config.yaml:1-19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/config.yaml#L1-L19)：pattern 三维矩阵的完整语义注释，含「同 epid 可跨 device 重复为 1 表示冗余」的关键说明。
- [components/omni-eplb/Guideline.md:96-110](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L96-L110)：两个使用要点——① pattern 必须与实际物理部署匹配（例中的 64 die pattern 只适用于 64 die 集群，其他规格参考 base_patterns 目录的 8/16/32/64/128/256 die 版本）；② 推荐 `pattern_path: null`，系统会自动匹配模型结构与物理部署形态生成默认 base_pattern。
- 仓库自带的真实 pattern 清单（`patterns/` 目录实际文件）：`pangu505B_128K_decode_64dieA3.npy`、`pangu505B_8K_decode_64dieA3.npy`（505B 的 decode 侧、64 die A3 集群，且区分 8K/128K 两种序列长度档位）、`A2_900_placement_pattern_20250904_2k2k_58_rearrange_layers_58_layers_8_ranks_prefill.npy` 等——文件名本身编码了「日期_模式_层数_rank 数_侧别」信息，可直接当命名规范参考。

wheel 的构建为什么必须在推理容器内做？看 setup.py：

- [components/omni-eplb/setup.py:24-26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/setup.py#L24-L26)：`ASCEND_TOOLKIT_HOME` 未设置直接抛 `EnvironmentError`——构建期就要找 CANN 头文件与库。
- [components/omni-eplb/setup.py:34-45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/setup.py#L34-L45)：扩展模块的 10 个 C++ 源文件（placement_manager、expert_load_balancer、dynamic_eplb_greedy、expert_activation、moe_weights 等）——核心均衡算法大量在 C++ 侧实现。
- [components/omni-eplb/setup.py:75-93](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/setup.py#L75-L93)：pybind11 扩展 `omni_placement.omni_placement`，链接 `hccl`、`ascendcl`、`torch`——动态重排需要 HCCL 集合通信搬运权重，激活采集走 ascendcl。这决定了它无法在普通 x86 开发机上编译出可用产物。
- [components/omni-eplb/Guideline.md:58-63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L58-L63)：在容器内 `python setup.py bdist_wheel`，产物落 `dist/` 目录（示例为 aarch64 版本——昇腾环境是 ARM 架构）。

本仓库组件构建脚本的差异：`build/build.sh` 走的是**可编辑安装**而非打包分发：

- [components/omni-eplb/build/build.sh:214-222](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/build/build.sh#L214-L222)：`pip_install_omni_placement_whl` 实际执行 `pip install -e .`——改代码即时生效，适合开发调试；Guideline 的 `bdist_wheel` 路线适合把产物分发到多机。两者二选一即可。
- [components/omni-eplb/build/build.sh:36-67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/build/build.sh#L36-L67)：脚本会 source `~/.bashrc` 并在 `ASCEND_TOOLKIT_HOME` 缺失时自动探测 `/usr/local/Ascend/ascend-toolkit/latest` 兜底——这就是「必须在推理容器内跑」的自动化保障。

#### 4.2.4 代码实践

**实践：解读 pattern 文件名并匹配物理部署**

1. 实践目标：给定一个部署形态，能选出正确的 pattern 文件。
2. 操作步骤：
   - 列出仓库自带 pattern：`ls -la components/omni-eplb/patterns/ components/omni-eplb/patterns/base_patterns/`；
   - 对照文件名拆解要素：`pangu505B_128K_decode_64dieA3.npy` = 模型 505B / 序列长度档 128K / 侧别 decode / 规模 64 die A3；
   - 回答：若你的 505B 服务 decode 侧跑在 64 die 的 A3 集群、典型请求 8K 上下文，应选哪个？若 prefill 侧是 8 die，应选哪个？
3. 需要观察的现象：pattern 与「侧别 + die 数」强绑定，没有万能文件；`base_patterns/` 下的 DSV3 基线只按 die 数区分，是「未优化的均匀起点」。
4. 预期结果：decode 64die A3 且 8K 档选 `pangu505B_8K_decode_64dieA3.npy`；prefill 8 die 参考带 `8_ranks_prefill` 字样的文件，或按 Guideline 推荐直接 `pattern_path: null` 让系统生成默认 base_pattern。序列档位如何影响选择属于经验问题，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 config.yaml 要准备 `config_p.yaml` 与 `config_d.yaml` 两份？

答案：两个原因。其一，物理形态不同：P 侧与 D 侧的 die 数、rank 数通常不同（如 P 是一个大 TP 实例、D 是多个小 server 的 DP 集群），pattern 的 deviceid 维度必须各自匹配（Guideline 96-110 行明确 pattern 须匹配物理部署）。其二，负载特征不同：prefill 侧激活由长文本批量统计主导、decode 侧由高并发小步批次主导，两侧的热门专家分布不同，dump 数据要分开收集、pattern 工具也用 `--collecting_modes prefill/decode` 分开生成（`patterns/` 目录的文件名 `_prefill`/`_decode` 后缀即证据）。所以 P/D 各配一份，各自指向各自的 pattern。

**练习 2**：`expert_mapping[deviceid][layerid][epid] = 1` 且同一 (layerid, epid) 在 3 个 device 上为 1，这代表什么？显存有什么变化？

答案：该层该专家部署了 3 个物理副本（冗余度 3），三个 rank 都要为它分配权重显存；路由到该专家的 token 可在三个副本间分摊。引擎侧的显存变化可在 [components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors_moe.py:79-88](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors_moe.py#L79-L88) 看到证据：`local_num_experts += num_of_redundant_experts`，即该 rank 的本地专家数直接加上冗余副本数，权重张量按扩大后的数量分配。

### 4.3 config.yaml 配置项精读

#### 4.3.1 概念说明

`config.yaml` 是 OmniPlacement 的唯一特性配置入口。它被 `Config` 类加载成对象属性，再由 `OmniPlanner.__init__` 逐字段消费。理解这份文件的正确姿势是「三问」：这个键控制什么？不写会怎样（默认值）？谁在读它（消费点）？

#### 4.3.2 核心流程

配置的加载与消费链路：

```
config.yaml（YAML 文本）
   │  yaml.safe_load
   ▼
Config 对象（dict 递归转属性，可 config.getattr(key, default) 取值）
   │
   ▼
OmniPlanner.__init__ 逐字段消费：
   max_moe_layer_num ──► 未配置时回退 num_layers - first_k_dense_replace，两者皆无则报错退出
   enable_dynamic ──► 动态模式总开关（ False 时 max_redundant_* 被置 None）
   enable_rank_round_robin ──► 未配置直接报错退出（必填项）
   pattern_path ──► 经 ExpertMapping 加载并校验 world_size
   enable_dump / dump_dir ──► rank0 按时间戳建 dump 目录
```

#### 4.3.3 源码精读

**配置文件本体**（[components/omni-eplb/config.yaml:20-51](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/config.yaml#L20-L51)），逐项含义：

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `pattern_path` | `null` | pattern 文件路径；null 时自动匹配模型结构与物理部署生成默认 base_pattern（Guideline 推荐用法） |
| `max_moe_layer_num` | 58 | MoE 层数上限（注释说明 58 来自 DeepSeek 的 58 个 MoE 层） |
| `enable_dynamic` | False | 动态均衡总开关 |
| `max_redundant_per_expert` | 1（注释建议冗余模式用 10） | 每个专家的冗余副本数上限，最大不超过总 die 数 |
| `max_redundant_per_rank` | 0（注释建议冗余模式用 1） | 每张卡（die）上允许的冗余专家个数上限 |
| `enable_new_context` | False | 为 eplb 单独创建新 HCCL 通信上下文（避免与主通信域相互干扰） |
| `enable_rank_round_robin` | True | rank 间轮询策略开关 |
| `enable_dump` / `dump_dir` | False / `../dump_data` | 静态模式第一步：是否 dump 激活数据及输出目录 |
| `max_batch_size` / `max_top_k` | 100000 / 8 | 预留的批与 top-k 上限（与激活统计缓冲相关） |
| `enable_zero_expert` / `normal_expert_ids` | False / 255 | 对 longcat 零号专家的适配（openPangu 不用） |
| `Optimizers` | 三个优化器 | 优化器列表：`expert_balance_optimizer.ExpertsBalanceOptimizer`、`heat_optimizer.HEAT_ExpertsBalancer`、`resdispatch_optimizer.ResDis_ExpertsBalancer` |

**加载器**：

- [components/omni-eplb/omni_placement/config.py:14-35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/config.py#L14-L35)：`Config.load_and_validate_config` 用 `yaml.safe_load` 读文件，找不到文件或 YAML 语法错误只打印信息并返回 None（**不抛异常**，排障时要看 stdout 的 `Attempting to read YAML file` 输出）。
- [components/omni-eplb/omni_placement/config.py:37-48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/config.py#L37-L48)：`_convert_dict_to_obj` 把嵌套 dict 递归转成对象属性，并暴露 `getattr(key, default)` 带默认值的读取接口。

**消费点**（OmniPlanner 构造函数）：

- [components/omni-eplb/omni_placement/omni_planner.py:58-79](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L58-L79)：构造参数含 `num_experts`、`first_k_dense_replace`、`num_layers` 等模型信息；`config_file` 为空时回退到**包安装目录旁的 config.yaml**（`current_file_path.parent.parent / "config.yaml"`）——这就是 4.4 节「配置注入」要改写的那份文件。
- [components/omni-eplb/omni_placement/omni_planner.py:85-90](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L85-L90)：`max_moe_layer_num` 未配置时回退 `num_layers - first_k_dense_replace`（呼应 u3-l1：前段是稠密层、其余才是 MoE 层）；两条路都没有则打印错误并 `exit(1)`。
- [components/omni-eplb/omni_placement/omni_planner.py:95-107](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L95-L107)：`enable_dynamic`、`enable_new_context`、`enable_rank_round_robin`（必填，缺失 exit(1)）、`max_redundant_per_rank/expert`（仅动态模式下生效，否则置 None）的逐项消费。
- [components/omni-eplb/omni_placement/omni_planner.py:109-114](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L109-L114)：`ExpertMapping` 依据 `pattern_path` 加载/生成部署矩阵，并硬校验 pattern 的 world_size 必须等于实际 world_size，不等则 exit(1)——**pattern 与物理部署不匹配会直接拒绝启动**。
- [components/omni-eplb/omni_placement/omni_planner.py:122-139](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L122-L139)：`enable_dump` 需要 `dump_dir` 同时存在才真正生效；仅 rank0 在 dump_dir 下按时间戳建子目录，每次 dump 的数据落在对应时间戳文件夹（对应 Guideline 静态模式 Step 2 描述的目录形态）。

**关于 Optimizers 列表的一个源码事实**（避免误配）：

- [components/omni-eplb/omni_placement/omni_planner.py:116-120](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L116-L120)：`self.optimizers = _create_optimizers(self.config.Optimizers, ...)` 这一行当前被注释（标注「TODO: 无效代码」）。也就是说 config.yaml 里的 `Optimizers` 列表在 Python 侧主路径中并未被加载，均衡决策主要走 C++ 侧（4.2.3 节的 placement_manager 等源文件）。加载器本身仍在：
- [components/omni-eplb/omni_placement/optim/optimizers_loader.py:6-48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/optim/optimizers_loader.py#L6-L48)：`create_optimizer_by_name` 按「模块名.类名」动态 import 并实例化，`_create_optimizers` 遍历配置列表逐个创建——机制完好，只是主流程暂未调用。改这个列表期望立刻换优化器的话，**以运行为准，不要想当然**。

#### 4.3.4 代码实践

**实践：配置三种典型场景的 config.yaml**

1. 实践目标：能按场景写出正确的最小配置组合。
2. 操作步骤：
   - 场景 A（静态均衡-采集阶段）：`enable_dump: true`、`dump_dir: "/home/profiling/dump_data"`、`enable_dynamic: False`；
   - 场景 B（静态均衡-应用阶段）：`enable_dump: false`、`pattern_path: "<绝对路径>/xxx_rearrange_layers_58_layers_16_ranks_prefill.npy"`；
   - 场景 C（动态冗余）：`enable_dynamic: True`、`max_redundant_per_expert: 10`、`max_redundant_per_rank: 1`。
   - 写完后与 Guideline 对应章节核对：[components/omni-eplb/Guideline.md:139-151](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L139-L151)（dump）、[170-176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L170-L176)（应用 pattern）、[187-197](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L187-L197)（动态冗余参数）。
3. 需要观察的现象：三个场景互斥的关键键；`pattern_path` 必须绝对路径；动态冗余两个上限参数要同时给。
4. 预期结果：场景 C 中 `max_redundant_per_expert: 10` 表示每个专家最多 10 个副本（典型值，不超过总 die 数），`max_redundant_per_rank: 1` 表示每 die 最多再放 1 个冗余专家（[components/omni-eplb/Guideline.md:192-196](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L192-L196) 的参数说明）。配置是否真正生效以服务启动日志回显为准，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `pattern_path` 指向一个 16 rank 的 pattern，但实际部署是 8 rank，会发生什么？

答案：启动即失败。[omni_planner.py:112-114](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L112-L114) 在 `ExpertMapping` 加载后硬校验 `get_world_size() != world_size` 就打印 `[Placement-Error]` 并 `exit(1)`。这是「pattern 必须匹配物理部署」约束的程序化兜底。

**练习 2**：`enable_dynamic: False` 时把 `max_redundant_per_expert` 设成 10，会发生什么？

答案：不生效。[omni_planner.py:106-107](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L106-L107) 用条件表达式写成 `... if self.enable_dynamic else None`——静态模式下两个冗余上限直接被置 None，冗余能力关闭。这也解释了 Guideline 3.4 节为什么把 `enable_dynamic: True` 列为动态重排/动态冗余的第一步。

**练习 3**：`enable_dump: true` 但忘了写 `dump_dir`，dump 会开启吗？

答案：不会。[omni_planner.py:122-123](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L122-L123)：`self.enable_dump = getattr(self.config, 'enable_dump', False) if dump_dir else False`——`dump_dir` 不存在时整个开关被短路为 False，且不报错。静默失效是这份配置里最需要警惕的行为模式。

### 4.4 启用链路与约束条件

#### 4.4.1 概念说明

本节回答三个问题：开源仓里 OmniPlacement 怎么被引擎启用？Guideline 的启用步骤和本仓库有什么对应关系？约束条件背后的原理是什么？

先给结论性的链路图（开源仓视角）：

```
vllm serve --enable-eplb --additional-config '{"omni_placement_config": {...}}'
   │
   ▼ NPUWorker.init_device 末尾
_init_omni_eplb_configs()
   ├─ 门禁1: additional_config 里有 "omni_placement_config" 键
   ├─ 门禁2: parallel_config.enable_eplb 为 True（即 --enable-eplb）
   └─ local_rank==0: apply_omni_eplb_attributes()
        └─ 把 omni_placement_config 的键值合并写入包内 config.yaml
   │
   ▼ 模型构建时（每个 MoE 层）
MoE 量化方法层 init_eplb()
   └─ OmniPlanner() 单例 ← 读包内 config.yaml → ExpertMapping 加载 pattern
        └─ local_num_experts += 冗余专家数（权重按扩大后的数量分配）
   │
   ▼ 引擎每步调度
patch_eplb_parallel 补丁接管 vLLM EplbState
   └─ step(): start_dynamic_optimize_expert_load_balance() + place_experts()
```

约束条件（Guideline 1.2 节四条）的原理：

1. **NPU/GPU 节点**：C++ 扩展链接 hccl/ascendcl，激活采集与权重搬运依赖设备运行时。
2. **safetensors / HF Transformers 格式**：`init_dram_weights` 要按模型参数名装载权重做重排搬移。
3. **需要 PD 分离部署**：负载均衡策略的收益建立在 P/D 职责分离的前提下（本仓库所有部署形态天然满足）。
4. **冗余部署与 AllGather 通信模式互斥**：u3-l3 讲过 MoE 的 EP 通信有 all2all、agrs（AllGather + ReduceScatter）等策略。agrs 模式下每个 rank 都会拿到全量 token 再本地过滤，专家分布的「物理位置」被通信模式抹平了语义；而冗余部署恰恰要求「token 按副本位置就近分发」，两者的路由假设冲突，所以 Guideline 明文规定不能同时开启。引擎侧开关对应 u3-l3 提过的 `moe_comm_strategy` 类配置——启用冗余前必须确认通信策略不是 AllGather 系。

#### 4.4.2 核心流程

静态均衡的完整操作流程（Guideline 3.3 节五步）：

```
Step1 config.yaml: enable_dump=true, dump_dir=...（P/D 两侧同配）
Step2 跑服务发请求 → 各服务器 dump_dir 生成 时间戳/activation_counts_recordstep_*.txt
     ├─ Decode 节点0 的 dump 文件夹 → Decode_path
     └─ 各 Prefill 节点的 prefill 文件夹 → P0_path, P1_path, ...
Step3 omni_pattern_tool/pattern_generation_pipeline.sh
     --input_txt_folders "<Decode_path>|<P0_path>/prefill ..." \
     --num_ranks_target_pattern <目标die数> --collecting_modes <decode|prefill>
     └─ 产出两份 pattern：..._rearrange_...npy 与 ..._redundant_...npy
Step4 config.yaml: enable_dump=false, pattern_path=<生成的 pattern 绝对路径>
Step5 重启服务，观察均衡表现并调优
```

动态模式的操作则简单得多：`enable_dynamic: True` 加两个冗余上限参数，启动后由引擎每步 step 驱动（见 4.4.3 第三块源码），无需 dump/pattern/重启。

#### 4.4.3 源码精读

**接入点 1：worker 侧配置注入**

- [components/omni-npu/src/omni_npu/worker/npu_worker.py:162-163](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_worker.py#L162-L163)：`NPUWorker.init_device` 末尾调用 `_init_omni_eplb_configs(self.vllm_config, self.local_rank)`——发生在设备初始化之后、模型加载之前，正好赶在 MoE 层构造 OmniPlanner 之前把配置落盘。
- [components/omni-eplb/omni_placement/utils.py:223-244](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/utils.py#L223-L244)：`_init_omni_eplb_configs` 的两道门禁——`additional_config` 不含 `omni_placement_config` 键直接返回；`parallel_config.enable_eplb` 不为 True 直接返回。**这就是本仓库的启用开关：`--enable-eplb` + `--additional-config` 里带 `omni_placement_config` 字典**（区别于 Guideline 描述上游仓的 `use_omni_placement: true`）。
- [components/omni-eplb/omni_placement/utils.py:189-221](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/utils.py#L189-L221)：`apply_omni_eplb_attributes` 读包内 `config.yaml`（[utils.py:17-18](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/utils.py#L17-L18) 定义的 `DEFAULT_YAML_PATH = BASE_DIR / "config.yaml"`），把 `omni_placement_config` 字典逐键覆盖合并后写回（`pattern_path` 的字符串 "null"/空串会被归一化为 None）。所以命令行 `--additional-config` 里的 eplb 配置最终会**实体化到包内 config.yaml**，随后被 OmniPlanner 读到。
- [components/omni-eplb/Guideline.md:83-124](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L83-L124)：Guideline 的启用章节，分 Option 1（D 侧）与 Option 2（P 侧）对称描述——再次印证两侧各自一份配置文件；V0.6.0 之前版本走 `use_omni_placement: true` + `omni_placement_config_path` 的旧开关。

**接入点 2：MoE 层消费**

- [components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors_moe.py:60-77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors_moe.py#L60-L77)：`init_eplb` 在每个 MoE 层构造时执行：`enable_eplb` 为 True 且层名不以 `mtp` 开头（MTP 层不参与均衡，呼应 u3-l5 的 draft 层）才启用；构造 `OmniPlanner` 单例（传 rank/world_size/num_experts），再按层名解析本层 MoE 层号并取本层的 `expert_mapping`。
- [components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors_moe.py:79-88](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors_moe.py#L79-L88)：查询本层冗余专家数，并把 planner、expert_mapping、moe_layer_idx 回填到 layer 上，最后 `layer.local_num_experts += self.num_of_redundant_experts`——冗余副本在这一行落实为真实的权重显存。

**接入点 3：运行时补丁驱动动态重排**

- [components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py:5-6](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py#L5-L6)：文件头注释给出启用方式——`VLLM_PLUGINS="omni-npu,omni_npu_patches" OMNI_NPU_VLLM_PATCHES="EPLBEngineConfig,EPLBSharedFusedMoE"`（u2-l4 讲过的补丁按注册名点名机制）。
- [components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py:23-28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py#L23-L28)：`@register_patch("EPLBState", EplbState)` 注册补丁，`_attr_names_to_apply` 一次性替换 `__init__`、`step`、`rearrange` 等 8 个符号——即把 vLLM 原生的 EplbState 行为整体替换为 NPU 版。
- [components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py:49-63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py#L49-L63)：`add_model` 中 `OmniPlanner()`（单例）+ `planner.init_dram_weights(param_dict, first_k_dense_replace=...)`——把模型权重登记进 planner，为后续搬移做准备；draft runner（MTP）跳过。
- [components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py:65-77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py#L65-L77)：`step()` 每步执行——首次调用 `start_dynamic_optimize_expert_load_balance()` 启动动态优化，之后每步 `place_experts()`。**这就是「近实时动态重排」在引擎侧的心跳**：vLLM 每个调度步都会经过这里。

**静态模式 pattern 流水线**：

- [components/omni-eplb/utils/omni_pattern_tool/pattern_generation_pipeline.sh:13-55](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/utils/omni_pattern_tool/pattern_generation_pipeline.sh#L13-L55)：流水线脚本的全部默认参数——输入 dump 文件夹（txt 模式）、`NUM_POSITIONS_OF_ROUTED_EXPERTS=256`（专家位 256）、`PATTERN_MODE=all`（同时生成 rearrange 与 redundant）、`COLLECTING_MODES=decode` 等；它串起 step_1 统计→step_2 生成→step_3 校验→step_4 收益分析四个 Python 步骤。
- [components/omni-eplb/Guideline.md:155-168](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L155-L168)：最小运行示例与三个核心参数（`--input_txt_folders` 支持空格分隔多文件夹、`--num_ranks_target_pattern` 是目标 die 数、`--collecting_modes` 区分 decode/prefill），产出文件名带 `rearrange`/`redundant` 字样。

#### 4.4.4 代码实践

**实践：在推理容器内完成 wheel 构建与 P/D 双侧启用配置（本讲主实践）**

1. 实践目标：走通「构建 → 安装 → 配置 → 启用」全链路，并能说清 P/D 分别配置的原因与约束条件。
2. 操作步骤：
   - ① 进入推理容器（沿用 u1-l4 的容器名约定，P 节点为例）：`docker exec -itu root w_omni_infer_prefill_p0 bash`；
   - ② 构建安装（二选一）：
     - 分发路线：`cd <仓库>/components/omni-eplb && python setup.py bdist_wheel && pip install dist/omni_eplb-*.whl`（若报 `ASCEND_TOOLKIT_HOME` 未设置，先 `source build/build.sh` 同款的环境兜底逻辑或手动 `export ASCEND_TOOLKIT_HOME=/usr/local/Ascend/ascend-toolkit/latest`）；
     - 开发路线：`bash build/build.sh`（可编辑安装，改码即生效）；
   - ③ 准备 P/D 两份配置：`cp config.yaml config_p.yaml && cp config.yaml config_d.yaml`，两侧都设 `enable_dump: true` 与自己的 `dump_dir`（静态采集起步），或直接 `enable_dynamic: True`（动态推荐路线）；
   - ④ 以动态模式启用：在 vllm serve 命令（即 ansible 模板的 `run_vllm_server_prefill_cmd`/`decode_cmd`）上追加 `--enable-eplb` 与 `--additional-config '{"omni_placement_config": {"enable_dynamic": true, "max_redundant_per_expert": 10, "max_redundant_per_rank": 1, "pattern_path": null}}'`；同时确保通信策略不是 AllGather 模式（u3-l3 的 moe_comm_strategy 相关配置）；
   - ⑤ 重启服务。
3. 需要观察的现象：启动日志先出现 `Enable omni-eplb, applying omni-eplb extra configurations to yaml file.` 与 `Successfully updated YAML file content:`（`apply_omni_eplb_attributes` 的回显），随后是 `[Info] Config file loaded from:` 与 pattern 加载/world_size 校验信息；若忘了 `--enable-eplb`，日志不会报错但也不会出现上述任何 eplb 回显（门禁静默返回）。
4. 预期结果：P、D 两侧日志各自回显 eplb 配置加载成功；两侧 config 不同（pattern 匹配各自 die 数）。完整跑通需要真实 NPU 集群，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 P 侧和 D 侧要分别配置 eplb？从物理形态与负载特征两方面回答。

答案：物理形态上，pattern 的 deviceid 维度必须匹配各自的 die/rank 数（P 侧通常是一个大 TP 实例、D 侧是 DP 集群，规模不同），world_size 校验不一致会直接 exit(1)；负载特征上，prefill 侧激活分布由长文本统计决定、decode 侧由高并发小批次决定，热门专家集合不同，需要各自独立的 pattern（`patterns/` 目录文件名的 `_prefill`/`_decode` 后缀与 pattern 工具的 `--collecting_modes` 参数都是这一点的直接证据）。

**练习 2**：「冗余部署和 AllGather 通信模式不支持同时开启」的原因是什么？

答案：agrs（AllGather+ReduceScatter）模式下每个 rank 汇聚全量 token 再本地过滤，路由分发依赖「所有 rank 都能看到所有 token」这一前提；而冗余部署的路由语义是「按副本位置把 token 就近分给某个副本所在 rank」，需要对分发目标做精确控制。两种路由假设冲突，同时开启会产生错误的路由/合并结果，因此 Guideline 1.2 节将其列为硬约束（[components/omni-eplb/Guideline.md:16-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/Guideline.md#L16-L21)）。

**练习 3**：开源仓的启用开关与 Guideline 写的 `use_omni_placement: true` 为何不同？以源码为证。

答案：Guideline 面向上游内部仓 omni_infer 的配置文件体系；本开源仓中，omni-npu 在 [npu_worker.py:162-163](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_worker.py#L162-L163) 调用 `_init_omni_eplb_configs`，其门禁读的是 vLLM 标准 `parallel_config.enable_eplb`（对应 `--enable-eplb`）与 `additional_config["omni_placement_config"]`（对应 `--additional-config`），见 [utils.py:223-244](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/utils.py#L223-L244)。所以在本仓库部署应以 `--enable-eplb --additional-config '{"omni_placement_config": {...}}'` 为准，Guideline 的旧开关仅适用于上游版本。

## 5. 综合实践

**任务：为你的 1P1D 服务设计一套 OmniPlacement 启用方案文档。**

以 u1-l4 已拉起的 1P1D BF16 服务为对象，产出一份包含以下内容的 Markdown 方案（放进你自己的运维文档目录，不要改动仓库源码）：

1. **构建与安装方案**：容器内 `bdist_wheel` 与 `build.sh` 可编辑安装两条路线的命令、前置环境变量（`ASCEND_TOOLKIT_HOME`）、以及为什么不能在普通 x86 开发机上编译（依据：setup.py 链接 hccl/ascendcl、aarch64 产物）。
2. **P/D 双侧配置表**：`config_p.yaml` 与 `config_d.yaml` 的完整键值，标注每个键的消费者（对应 4.3.3 节的 OmniPlanner 消费点行号），并说明两侧 pattern_path 应如何按各自 die 数选择（或设 null 自动生成）。
3. **启用命令草案**：在 ansible 模板 `run_vllm_server_prefill_cmd`/`decode_cmd` 中追加的 `--enable-eplb` 与 `--additional-config` 片段；对照 u2-l4 的补丁机制，补上 `OMNI_NPU_VLLM_PATCHES` 环境变量应点名的补丁注册名。
4. **约束检查清单**：逐条核对 Guideline 1.2 的四条约束，特别是当前部署的 MoE 通信策略是否为 AllGather 系（回看 u3-l3 的三种 EP 通信策略），冗余部署前必须确认。
5. **验证与回退**：预期日志关键字（`Enable omni-eplb`、`Successfully updated YAML file`、pattern world_size 校验通过）、验证均衡效果的观察指标、以及回退步骤（去掉 `--enable-eplb` 与 additional-config 键即可回到基线，因为门禁不过时 `_init_omni_eplb_configs` 直接 return，不产生副作用）。

没有真机时，第 1、2、3、4 项均可纸面完成，第 5 项标注「待本地验证」。

## 6. 本讲小结

- MoE 专家负载不均源于模型学到的路由偏好，EP 组的 MoE 耗时由最慢 rank 决定（木桶效应），均衡目标即 \(\min \max_d A[d]\)。
- OmniPlacement 四板斧：专家重排（零显存搬位置）、层间不均匀部署（逐层独立矩阵）、冗余专家（副本分摊 + 高可用降 RTO）、近实时激活采集（静态 dump 与动态决策共用）。
- 部署三件套 = wheel（含链接 hccl/ascendcl 的 C++ 扩展，必须在推理容器内构建）+ P/D 各一份 config.yaml + pattern 文件（三维 0/1 矩阵，多 device 同为 1 即冗余，world_size 不匹配直接拒绝启动）。
- config.yaml 关键行为：静态模式下冗余上限被置 None；`enable_dump` 依赖 `dump_dir` 存在否则静默关闭；`Optimizers` 列表的 Python 侧加载调用当前被注释，均衡主路径在 C++ 侧。
- 本仓库启用链路：`--enable-eplb` + `--additional-config` 的 `omni_placement_config` 键 → `_init_omni_eplb_configs` 门禁 → 合并写入包内 config.yaml → MoE 层 `init_eplb` 构造 OmniPlanner 并扩容 local_num_experts → `patch_eplb_parallel` 补丁接管 vLLM EplbState 的 step 驱动动态重排。
- 硬约束：冗余部署与 AllGather 通信模式互斥；本仓库 ansible 模板尚无 eplb 接线，需按 Guideline 手工接入。

## 7. 下一步学习建议

本讲只到「会用、会配」为止，三个核心类的内部实现还是黑盒。下一讲 **u9-l2「OmniPlanner 与专家重排实现」**将沿调用链拆开：

- `omni_planner.py` 的规划循环与 `start_dynamic_optimize_expert_load_balance`/`place_experts` 的完整执行过程；
- `expert_mapping.py` 的物理/逻辑专家映射表示与 pattern 生成/加载；
- `placement_handler.py` 的权重热搬移与 `cluster_status.py` 的集群状态维护；
- 以及 `patch_eplb_parallel` 与 vLLM EplbState 的衔接细节。

阅读本讲时建议同步打开这三个文件对照，先记住它们在本讲链路图中出现的位置即可。
