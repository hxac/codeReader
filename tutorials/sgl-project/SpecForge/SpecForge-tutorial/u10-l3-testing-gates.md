# 测试与质量门禁

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 SpecForge 的测试体系是**怎样分层的**，每个 `tests/test_*` 子目录对应源码包的哪一块，以及为什么策略类测试大多落在 `test_runtime` 而不是 `test_training`。
- 看懂 `scripts/gates/run_disaggregated_overfit_gate.sh` 这条**端到端（e2e）质量门禁**到底在校验哪几类行为：从选一个样本、起 Mooncake 和 patched SGLang、跑 producer/consumer、卡严格 loss/精度，到导出与真实服务的验收。
- 理解 `.github/workflows` 里的两条 CI（GPU 上的 `test.yaml`、ubuntu 上的 `lint.yaml`）和 `.pre-commit-config.yaml` 本地钩子如何共同守住「提交前不脏、合并前能跑」的质量底线。
- 在自己新增一个算法（如 `mydraft`）时，知道**最少要补齐哪几个测试目录**，并能复用现成的 gate 脚本做端到端验证。

本讲是「扩展与二次开发」单元的收尾，回答一个工程问题：**我改了/加了一块代码，怎么确信没有把别处搞坏？**

## 2. 前置知识

本讲默认你已经读过：

- **u1-l5 目录结构与源码地图**：知道 `specforge/` 下 `algorithms / application / config / data / modeling / runtime / training / inference` 各子包的职责。
- **u3 入口与启动链路**：知道一次 `specforge train` 经过 `cli → config → composition → launch → assembly → trainer`，以及 `launch_plan` 的 worker/supervisor 分流。
- **u4 算法注册与契约**：知道一个算法由「纯契约 `AlgorithmSpec` + 可执行 `providers` + 训练策略 `DraftTrainStrategy`」三件套组成。
- **u6 训练主链路** 与 **u7 DataFlow 运行时**：知道 trainer 一步怎么算 loss、在线 disaggregated 的 producer/consumer 如何协同。
- **u9 导出、评测与基准**：知道 `specforge export` 把训练态检查点物化为服务目录。

三个术语先对齐：

- **单元测试（unit test）**：对单个函数/类的小颗粒检查，不依赖 GPU、不启动外部服务。
- **端到端门禁（e2e gate）**：拉起整套真实依赖（SGLang、Mooncake、多进程），跑一条最小但完整的训练 + 服务链路，断言关键数值。
- **质量门禁（quality gate）**：任何「不满足就阻止合并/发布」的自动化检查，含 lint、单元测试、e2e gate。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| `tests/test_*/` | 单元与集成测试，按源码包分层，用 `unittest` 编写 |
| [tests/utils.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/utils.py) | 测试共享工具：端口探测、起进程、等 server 就绪、清理进程树 |
| [scripts/gates/run_disaggregated_overfit_gate.sh](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/run_disaggregated_overfit_gate.sh) | 一条命令的端到端 overfit + 服务门禁编排 |
| [scripts/gates/_e2e_common.sh](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/_e2e_common.sh) | gate 共享的 bash 工具：参数校验、dry-run、起/停服务、清理 trap |
| [scripts/gates/check_overfit_metrics.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/check_overfit_metrics.py) | 叶子检查器：卡最终 step / loss / 精度 / 检查点 |
| [.pre-commit-config.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.pre-commit-config.yaml) | 本地提交前钩子：格式化、静态检查、防提交大文件/私钥/到主分支 |
| [.github/workflows/test.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.github/workflows/test.yaml) | PR 上的 GPU CI：装环境 → 实捕 gate → 全量 unittest |
| [.github/workflows/lint.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.github/workflows/lint.yaml) | PR 上的 lint CI：跑 `pre-commit run --all-files` |

## 4. 核心概念与源码讲解

### 4.1 tests 分层：镜像源码包的测试网格

#### 4.1.1 概念说明

SpecForge 的测试不依赖任何第三方测试框架，全部用 Python 标准库的 `unittest` 编写。一个重要直觉是：**测试目录是源码包的镜像**。源码里有一个 `specforge/algorithms/`，测试里就有一个 `tests/test_algorithms/`；源码里有 `specforge/runtime/data_plane/`，测试里就有 `tests/test_runtime/test_feature_store.py`、`test_ref_distributor.py`。这种一一对应让你在改某个源码文件时，能立刻知道该去哪个测试目录加用例。

测试被刻意分成两类：

- **轻量单元测试**：纯 CPU、不连 GPU、不启动服务，几秒内跑完，覆盖契约、配置、数据解析、算子数值。这是绝大多数测试。
- **重活测试（gate/smoke）**：需要 GPU 或外部 SGLang/Mooncake，靠环境变量开关 opt-in，CI 里单独触发。

#### 4.1.2 核心流程

CI 用一条命令发现并跑全部测试（见 [.github/workflows/test.yaml:74-80](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.github/workflows/test.yaml#L74-L80)）：

```bash
python -m unittest discover -s ./tests -p "test_*.py" -v
```

`discover` 会扫描 `tests/` 下所有匹配 `test_*.py` 的文件，收集其中 `unittest.TestCase` 的子类里以 `test_` 开头的方法。没有 `pytest`、没有 `conftest.py`、没有 fixture 魔法——一个测试就是一个类方法，断言用 `self.assertEqual` / `self.assertRaises`。

各子目录与源码的对应关系如下（按覆盖范围排列）：

| 测试目录 | 文件数（约） | 对应源码 | 典型关注点 |
| --- | --- | --- | --- |
| `test_algorithms/` | 6 | `algorithms/` | `AlgorithmSpec` 纯值校验、registry resolve、`make_registration` parity、离线 capture layout |
| `test_application/` | 1 | `application/` | 组合根 `resolve_run`、训练拓扑校验 |
| `test_config/` | 7 | `config/` | 七段 schema、未知字段报错、示例 YAML 可达性、launch 拓扑 |
| `test_data/` | 9 | `data/` | chat template、parser、loss mask、prompt builder |
| `test_layers/` | 4 | `modeling/layers/` | linear / embedding / lm_head / decoder |
| `test_modeling/` | 3 | `modeling/draft/` | `AutoDraftModel`、`@register_draft`、Domino 模型 |
| `test_offline_capture/` | 1 | `offline_capture/` | SGLang 后端捕获 |
| `test_optimizer/` | 1 | `optimizer.py` | `BF16Optimizer` 梯度裁剪范数 |
| `test_runtime/` | 50+ | `runtime/` + `training/` | **最大的一袋**：契约、checkpoint、feature store、ref 分发、各策略、launch、等价性 |
| `test_training/` | 1 | `training/` | liger kernel 集成（策略测试大多落在 `test_runtime`） |
| `test_benchmarks/` | 2 | `benchmarks/` | sglang 基准、humaneval |
| `test_scripts/` | 9 | `scripts/` | 数据准备脚本、gate 编排、launcher |
| `test_utils/` | 10 | `core/` + 各 utils | loss 算子、分块、flash/flex attention、tokenizer 加载 |
| `tests/ci/` | 1 | （CI 辅助） | `gpu_lock_exec.py`，不是测试，是 CI 串行化 GPU 的工具 |

> **注意一个反直觉点**：训练策略（`DraftTrainStrategy` 的 `forward_loss` / `required_features`）的测试不在 `test_training/`，而在 `test_runtime/`（如 `test_peagle_strategy.py`、`test_compact_teacher_strategy.py`、`test_domain_trainer.py`）。原因是策略验证往往要连同 runtime 装配一起跑，自然归入 `test_runtime`。`test_training/` 只放了不需要 runtime 的零散集成（如 liger kernel）。

#### 4.1.3 源码精读

**测试共享工具 `tests/utils.py`** 提供了四件跨测试复用的能力。看端口探测：

[tests/utils.py:10-17](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/utils.py#L10-L17) —— `is_port_in_use` 试图 `bind` 一个端口，绑得上说明空闲；`get_available_port` 在 `10000..65535` 里找一个空闲端口，供测试动态起 server。

最值得讲的是进程清理 `terminate_process_trees`：

[tests/utils.py:64-82](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/utils.py#L64-L82) —— 它没有自己造轮子，而是**复用生产代码 `specforge.launch_plan._terminate_processes`**。注释点明原因：SGLang 的 launcher 进程会和它派生的 scheduler/model worker 各自独立退出，若只杀了 launcher，占着 GPU 的子进程会变成孤儿，污染下一个测试。把生产侧的进程组清理逻辑拿来用，保证「死掉的 leader 也不会留下活着的后代」。

[tests/utils.py:85-132](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/utils.py#L85-L132) —— `wait_for_server` 轮询 OpenAI 兼容的 `/v1/models` 端点，返回 200 再多睡 5 秒就认为就绪；可选地在轮询期间临时摘掉代理环境变量，避免本机代理拦截到 `localhost` 的请求。

**测试编写范式**：以 `test_algorithms/test_contracts.py` 为例，它构造最小契约对象再断言不变量：

[tests/test_algorithms/test_contracts.py:17-29](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_algorithms/test_contracts.py#L17-L29) —— 用工厂函数 `_offline()` 拼一个最小的 `FeatureContract`（OFFLINE 模式 + 一组 `required_tensors` + 一个 `OfflineStorageContract`），再喂给 `AlgorithmSpec` 做断言。这种「工厂函数造最小样本 → 断言不变量」是全仓库测试的通用写法。

**组合根测试**关注「配置 → 算法」的解析是否正确：

[tests/test_application/test_composition.py:9-13](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_application/test_composition.py#L9-L13) —— 直接 `from specforge.application import resolve_run`，把一个 payload 字典解析成 `ResolvedRun`，并额外 import 了 `_validate_training_topology` 来单独验证拓扑规则。

#### 4.1.4 代码实践

**实践目标**：建立「目录 ↔ 源码」的肌肉记忆，并为后续给新算法补测试做准备。

**操作步骤**：

1. 在仓库根执行 `ls tests/test_*` 列出全部测试子目录。
2. 对每个子目录执行 `ls tests/test_algorithms`（替换为各目录名），看里面有哪些 `test_*.py`。
3. 任选 3 个测试文件，打开看它的 import 行（从 `specforge.xxx import` 反推它在测哪个源码模块）。

**需要观察的现象**：你会发现文件名几乎就是源码文件名的「动词化」——`specforge/algorithms/contracts.py` 对应 `test_contracts.py`，`specforge/runtime/data_plane/feature_store.py` 对应 `test_feature_store.py`。

**预期结果**：你能填出下面这张「目录 → 职责」对照表（参考 4.1.2 的表格）。如果想验证某测试确实能跑且无需 GPU，可执行：

```bash
python -m unittest tests.test_algorithms.test_contracts -v
```

> 待本地验证：若你的环境未装 GPU 版 torch，`test_runtime` 里部分 import torch 的文件可能跳过或报错；纯契约/配置类测试（`test_algorithms`、`test_config`、`test_application`）通常纯 CPU 即可跑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `test_training/` 只有 1 个文件，而策略相关测试却散落在 `test_runtime/`？

**参考答案**：`DraftTrainStrategy` 的验证（`forward_loss`、`required_features`、checkpoint 过滤）大多需要连同 runtime 装配、feature store、trainer 循环一起跑才有意义，因此自然归入覆盖整个 runtime 的 `test_runtime/`；`test_training/` 只放不需要 runtime 的零散集成（如 liger kernel 替换）。

**练习 2**：`tests/utils.py` 的 `terminate_process_trees` 为什么不自己写杀进程逻辑，而要去 import 生产代码？

**参考答案**：SGLang 的 launcher 会独立于其 scheduler/model worker 退出，简单杀父进程会留下占 GPU 的孤儿。复用 `specforge.launch_plan._terminate_processes` 既避免重复实现，又保证测试与生产用同一套（已处理「死 leader 留活后代」的）进程组清理逻辑。

**练习 3**：CI 跑测试用的是 `pytest` 还是 `unittest`？发现命令是什么？

**参考答案**：用的是标准库 `unittest`，命令是 `python -m unittest discover -s ./tests -p "test_*.py" -v`。

---

### 4.2 e2e gate：一条命令的 overfit + 服务门禁

#### 4.2.1 概念说明

单元测试覆盖「单点正确」，但 SpecForge 的价值链很长：**配置解析 → 在线捕获 → Mooncake 传输 → producer/consumer 协同 → trainer 算 loss → checkpoint → 导出 → 服务加速**。任何一环接线错误，单点测试都可能是绿的。`scripts/gates/` 下的 e2e gate 就是为了堵这个缺口：用**一条命令**跑一条最小但完整的「垂直切片」，断言关键数值。

它的核心思想是 **overfit（过拟合）一个样本**：把训练数据缩减到**单条**样本，跑足够多步（默认 `MAX_STEPS=400`），要求最终 loss 降到极低（默认 `MAX_LOSS=0.0001`）、token 精度达到 1.0。如果整条链路有任何接线或数值错误，草稿模型绝不可能把单条样本背到 loss≈0。所以 overfit 通过 ≈ 「端到端数值正确」。

更妙的是它复用 u2-l1/u3 学过的**唯一训练入口**：gate 不自己调任何方法专属脚本，而是走规范包装器 `examples/disagg/run_online.sh`，后者 `exec specforge train`。这一点在 gate README 里被强调为「Training is launched only through the canonical wrapper」。

#### 4.2.2 核心流程

`run_disaggregated_overfit_gate.sh` 的执行流程（一条龙，全由脚本拥有和清理）：

```text
1. 校验环境变量（CONFIG / TARGET_MODEL_PATH / DRAFT_CONFIG_PATH / SOURCE_DATA_PATH 必填）
2. 从 draft config 解析 block_size / mask_token_id / projector_type / 捕获层 id
3. 计算 in-flight 水位、prompt 轮数（= MAX_STEPS × NPROC_PER_NODE）
4. select_overfit_sample.py：从数据集挑 1 条「不截断、符合 reasoning 策略、≥2×block_size 可训练 token」的样本
5. 起 Mooncake（元数据 + 共享存储）
6. 起 patched SGLang capture server（--enable-spec-capture）
7. 起 producer（--role producer，捕获特征写 Mooncake，发 SampleRef）
8. 起 consumer（--role consumer，跑训练，max_steps=400）
9. check_overfit_metrics.py：断言 step==400、loss≤1e-4、accuracy≥1.0、最终 checkpoint 存在
10. 释放 capture 栈
11. （可选）serving gate：specforge export --to hf → 起 SGLang DFLASH 服务 → 验收 spec_accept_length≥block_size 且目标前缀吻合
12. EXIT/INT/TERM trap：清理所有由本 gate 拥有的进程
```

步骤 7-8 复用了 u7 学过的「canonical 在线 disaggregated 流」：producer 把张量直写 Mooncake、只回 SampleRef 元数据；consumer 的 `RefDistributor` 按 quantum 分发，trainer 经 `FeatureDataLoader` 取回成 `TrainBatch`。gate 把这条流端到端验证一遍。

#### 4.2.3 源码精读

**脚本头部与用途**：[run_disaggregated_overfit_gate.sh:1-8](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/run_disaggregated_overfit_gate.sh#L1-L8) 点明这是「严格 DFlash 家族 disaggregated overfit + 可选服务门禁」，并 `source` 共享库 `_e2e_common.sh`。

**环境契约**：[run_disaggregated_overfit_gate.sh:59-68](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/run_disaggregated_overfit_gate.sh#L59-L68) 用一串 `gate_require_value` / `gate_require_file` 把四个必填变量与文件存在性卡死，缺一个立刻 `gate_fail` 退出——典型的 fail-fast。

**从 draft config 抽元数据**：[run_disaggregated_overfit_gate.sh:108-136](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/run_disaggregated_overfit_gate.sh#L108-L136) 内联一小段 Python 读 `dflash_config`，校验 `block_size`、`target_layer_ids`、`mask_token_id` 都是合法整数，并按 `EXPECTED_PROJECTOR_TYPE`（默认 `domino`）校验 projector 类型，再把这些值用 `|` 拼成一串回传给 bash。

**核心覆盖参数**：[run_disaggregated_overfit_gate.sh:216-250](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/run_disaggregated_overfit_gate.sh#L216-L250) 是 `COMMON_OVERRIDES` 数组——这就是 u2-l3 学过的 dotted overrides 的实战：把单样本路径、`max_steps=400`、`batch_size=1`、`accumulation_steps=1`、`tp/sp=1`、tracking 关闭、watermark 等以 `section.field=value` 形式塞给 `specforge train`。注意 `data.prompts_path=""` 这种把字段显式清空的写法。

**起服务 + producer/consumer**：[run_disaggregated_overfit_gate.sh:313-332](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/run_disaggregated_overfit_gate.sh#L313-L332) 用 `gate_start_service` 起 patched SGLang capture server，再分别以 `--role producer` / `--role consumer` 走 `run_online.sh`，consumer 的输出 tee 到 `train.log` 供后续检查。

**严格数值验收**：[run_disaggregated_overfit_gate.sh:334-344](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/run_disaggregated_overfit_gate.sh#L334-L344) 调 `check_overfit_metrics.py`，并断言最终 `{run_id}-step{MAX_STEPS}/training_state.pt` 存在。

具体阈值逻辑在叶子检查器里：

[check_overfit_metrics.py:58-88](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/check_overfit_metrics.py#L58-L88) —— `check_overfit` 从训练日志解析最后一行 metric（兼容 `[consumer] step N {...` 与统一 `step N: {...` 两种格式），收集所有错误：`step != expected`、`loss > max_loss`、`accuracy < min_accuracy`、无 checkpoint，任一不符就抛 `ValueError`。它是**方法无关**的：阈值 `--max-loss` / `--min-accuracy` 是 CLI 参数，未来新算法的 overfit gate 直接传不同值即可，无需改这个脚本。

**服务门禁链接**：[run_disaggregated_overfit_gate.sh:349-366](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/run_disaggregated_overfit_gate.sh#L349-L366) 在 `RUN_SERVING_GATE=true`（默认）时，把 checkpoint 路径等变量透传给 `run_dflash_serving_gate.sh`，做导出 + 真实服务验收。

**共享工具 `_e2e_common.sh`** 提供了 gate 的「脚手架」：

[_e2e_common.sh:9-12](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/_e2e_common.sh#L9-L12) —— `gate_fail` 统一以退出码 2 报错。[_e2e_common.sh:22-58](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/_e2e_common.sh#L22-L58) 是一组 `gate_require_*` 守卫：值非空、文件/目录存在、正/非负整数、命令在 PATH、TCP 端口空闲。

[_e2e_common.sh:128-154](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/_e2e_common.sh#L128-L154) —— `gate_start_service` 用 `setsid` 把服务放进独立进程组（`mode=group`），记录 PID/模式/标签到三个并行数组，供后续按组清理。

[_e2e_common.sh:271-325](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/_e2e_common.sh#L271-L325) —— `gate_stop_services` 是三段式清理：先对每个活进程发 `TERM`，轮询最多 50 次（每次 0.1s）等其退出，仍不退则发 `KILL` 并 `wait` 收尸。这保证 gate 结束时不留 GPU 占用的孤儿。

[_e2e_common.sh:334-338](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/_e2e_common.sh#L334-L338) —— `gate_install_cleanup_traps` 在 `EXIT/INT/TERM` 上挂 `gate_cleanup`，确保**任意退出路径**（正常、Ctrl-C、被 kill）都会清理。

**dry-run** 是 gate 的一大特色（[_e2e_common.sh:89-91](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/_e2e_common.sh#L89-L91) 与 [run_disaggregated_overfit_gate.sh:368-372](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/gates/run_disaggregated_overfit_gate.sh#L368-L372)）：`GATE_DRY_RUN=1` 时所有 `gate_run` / `gate_start_service` 只打印「shell-safe 的完整命令」而不真正执行，用于在不占 GPU、不起服务的前提下体检整张命令计划。

#### 4.2.4 代码实践

**实践目标**：用 dry-run 零成本体检 overfit gate 的完整命令链。

**操作步骤**：

```bash
GATE_DRY_RUN=1 \
CONFIG=examples/configs/qwen3-8b-domino-disaggregated.yaml \
TARGET_MODEL_PATH=Qwen/Qwen3-8B \
DRAFT_CONFIG_PATH=configs/qwen3-8b-domino.json \
SOURCE_DATA_PATH=./cache/dataset/sharegpt_train.jsonl \
bash scripts/gates/run_disaggregated_overfit_gate.sh
```

**需要观察的现象**：脚本会逐行打印它**将要执行**的每条命令（前缀 `+`），包括 `select_overfit_sample.py` 的完整参数、`COMMON_OVERRIDES` 里所有 dotted overrides、起 Mooncake / SGLang / producer / consumer 的命令、以及末尾的 `check_overfit_metrics.py`。末尾应打印 `DISAGG-OVERFIT-E2E-PLAN: <work_dir>`。

**预期结果**：不创建目录、不占 GPU、不起任何服务，只看到完整计划。这等于一次「配置 + 拓扑 + 命令构造」的体检。

> 待本地验证：若 `SOURCE_DATA_PATH` 指向的文件不存在，dry-run 会在 `gate_require_file` 处就 fail，不会进入命令打印阶段——这正是 fail-fast 守卫生效的表现。

#### 4.2.5 小练习与答案

**练习 1**：overfit gate 把 loss 阈值设到 `1e-4`、精度设到 `1.0`，这为什么能当成「端到端正确」的证据？

**参考答案**：单条样本经 400 步训练若能把 loss 压到 1e-4、token 精度到 1.0，说明草稿模型真正「背下」了这条样本——这只有在「捕获的特征正确 → 传输无错 → trainer 的 forward/backward 数值正确 → checkpoint 落盘正确」整条链路都成立时才可能。任一环节出错，loss 不可能降到这么低。

**练习 2**：gate 用什么机制保证「Ctrl-C 也不留 GPU 孤儿」？

**参考答案**：`gate_install_cleanup_traps` 在 `EXIT/INT/TERM` 信号上都挂了 `gate_cleanup`，它会调 `gate_stop_services`——先 `TERM`、轮询等待、不退再 `KILL` 并 `wait` 收尸，且因服务用 `setsid` 起在独立进程组，能整组清理包括 SGLang 的 scheduler/model worker 后代。

**练习 3**：为什么 gate 调训练用的是 `examples/disagg/run_online.sh` 而不是某个 `train_dflash.py`？

**参考答案**：SpecForge 只有唯一类型化训练入口 `specforge train`，没有方法专属脚本；`run_online.sh` 是它的薄包装（`exec specforge train`）。gate 复用规范入口，既避免新增训练入口，又保证 gate 验证的就是用户真实使用的路径。

---

### 4.3 CI 与 pre-commit：合并前后的两道闸

#### 4.3.1 概念说明

质量门禁分两道时间点：

- **提交前（本地，pre-commit）**：开发者 `git commit` 时触发，跑格式化与轻量静态检查，几秒内完成，**自动修复**大部分风格问题。钩子定义在 `.pre-commit-config.yaml`。
- **合并前（CI，GitHub Actions）**：提 PR 时触发，跑完整 lint（ubuntu）和 GPU 上的单元测试 + 实捕 gate（self-hosted GPU runner），几分钟到几十分钟，**只验证不修复**。定义在 `.github/workflows/`。

两者职责互补：pre-commit 把「低级脏」挡在提交前；CI 把「逻辑错」挡在合并前。

#### 4.3.2 核心流程

**lint 流（[.github/workflows/lint.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.github/workflows/lint.yaml)）**：ubuntu 最新版，装 Python 3.11 + pre-commit，跑：

```bash
pre-commit run --all-files --show-diff-on-failure
```

它和本地的 pre-commit 是**同一份配置**，所以「本地过了 CI 就大概率过」。

**test 流（[.github/workflows/test.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.github/workflows/test.yaml)）**：跑在 self-hosted GPU 上，容器是 `lmsysorg/sglang:v0.5.14-cu130`（锁定版本避免反复拉镜像），带 `--gpus all`、大 shm、privileged、`--pid=host`。分三步：

1. 装环境：`uv venv` + `torch==2.11.0 (cu130)` + `flash-attn` + 本仓 `uv pip install -v .` + `mooncake-transfer-engine-cuda13`。
2. **实捕 gate**：设 `SPECFORGE_RUN_SERVER_CAPTURE_TESTS=1`，打 SGLang patch，跑 `tests.test_runtime.test_server_capture_gate`——这是 u7-l5 推理面捕获的在线数值门禁。
3. **全量 unittest discover**：跑 `tests/` 下全部 `test_*.py`。

> 两条 CI 都开了 `cancel-in-progress`（lint）或 `concurrency` 串行队列（test），避免 GPU 资源被重复提交浪费。

#### 4.3.3 源码精读

**CI 容器与并发控制**：[.github/workflows/test.yaml:24-26](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.github/workflows/test.yaml#L24-L26) 锁定 sglang v0.5.14 + cu130 镜像，配大 shm 与 privileged 以支持 Mooncake 共享内存与 GPU。`concurrency: specforge-gpu-ci, cancel-in-progress: false, queue: max` 让 GPU 任务串行排队。

**实捕 gate 步骤**：[.github/workflows/test.yaml:62-72](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.github/workflows/test.yaml#L62-L72) 先断言 `torch.cuda.is_available()`、`mooncake.store` 可 import、`mooncake_master` 可找到，再跑 `tests.test_runtime.test_server_capture_gate`。这个测试的 docstring 把它校验的四件事讲得很清楚：

[tests/test_runtime/test_server_capture_gate.py:1-27](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_server_capture_gate.py#L1-L27) —— 它 pin 住：① 整条零拷贝传输（`/generate` 带 `spec_capture` → 服务端写 Mooncake → 只从 `meta_info` 拼 `SampleRef` → 真跑一步 eagle3 训练）；② 提取正确性（服务端捕的 hidden state 与独立 HF `output_hidden_states=True` 前向在 bf16 容差内一致）；③ 策略无关性（同一服务端既能服务 eagle3 也能服务 dflash）；④ 缓存隔离（radix cache 开启时同一 prompt 仍能逐 token 捕获）。本地需 GPU + 打 patch + mooncake + 显式 `SPECFORGE_RUN_SERVER_CAPTURE_TESTS=1` 才会真跑，否则被跳过（见 [test_server_capture_gate.py:42-46](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_server_capture_gate.py#L42-L46)）。

**全量测试步骤**：[.github/workflows/test.yaml:74-80](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.github/workflows/test.yaml#L74-L80) `python -m unittest discover`。

**pre-commit 钩子**（[.pre-commit-config.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.pre-commit-config.yaml)）分七组：

- [autoflake](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.pre-commit-config.yaml#L4-L8)：删未用 import。
- [pre-commit-hooks](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.pre-commit-config.yaml#L9-L27)：一篮子守卫——`trailing-whitespace`、`end-of-file-fixer`、`check-yaml`、`check-toml`、`check-ast`（语法能编译）、`check-added-large-files`、`check-merge-conflict`、`detect-private-key`（防泄密钥）、`debug-statements`（防遗留 `pdb`）、以及 `no-commit-to-branch`（默认保护 `main`/`master`，防直推主干）。
- [isort](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.pre-commit-config.yaml#L28-L31)：整理 import 顺序。
- [ruff](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.pre-commit-config.yaml#L32-L38)：**只**对 `docs/` 与 `examples/` 选 `F401`（未用 import）并自动修，不碰主源码。
- [black-jupyter](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.pre-commit-config.yaml#L39-L42)：格式化 Python 与 notebook。
- [clang-format](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.pre-commit-config.yaml#L43-L48)：格式化 C++/CUDA（triton/扩展内核）。
- [nbstripout](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.pre-commit-config.yaml#L49-L55)：提交 notebook 时剥输出。

[.pre-commit-config.yaml:1](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/.pre-commit-config.yaml#L1) 声明 `default_stages: [pre-commit, pre-push, manual]`，钩子在三个时机都能跑。

#### 4.3.4 代码实践

**实践目标**：在本地复现 CI 的 lint 闸，体验「提交前自动修」。

**操作步骤**：

```bash
# 1. 装 pre-commit（pyproject 的 dev extras 就是它）
pip install pre-commit          # 或: uv pip install -e ".[dev]"

# 2. 安装 git hook（把 .pre-commit-config.yaml 挂到 .git/hooks/pre-commit）
pre-commit install

# 3. 对全仓库跑一次（与 CI lint.yaml 完全一致）
pre-commit run --all-files --show-diff-on-failure
```

**需要观察的现象**：第一次跑会下载各钩子环境（较慢）；之后只对改动文件跑（秒级）。若某文件有尾随空格、未用 import、未格式化，钩子会**自动改写文件**并以非零退出码结束（`--show-diff-on-failure` 会显示它改了什么）。

**预期结果**：自动修复后，`git diff` 能看到钩子替你做的格式化；把改动 `git add` 再提交即可通过。`no-commit-to-branch` 还会阻止你直接往 `main` 提交。

> 待本地验证：若你尝试在 `main` 分支上 `git commit`，`no-commit-to-branch` 会直接拒绝；新建一个分支再提交即可。

#### 4.3.5 小练习与答案

**练习 1**：CI 的 `lint.yaml` 和本地的 pre-commit 是什么关系？

**参考答案**：两者用**同一份** `.pre-commit-config.yaml`。CI 跑 `pre-commit run --all-files`，本地 `pre-commit install` 后每次 commit 自动跑相同钩子，所以本地过了 CI 就基本能过。

**练习 2**：CI 为什么把 `test_server_capture_gate` 单独放在全量 unittest 之前，且要设 `SPECFORGE_RUN_SERVER_CAPTURE_TESTS=1`？

**参考答案**：这个 gate 要 GPU + 打过 patch 的 SGLang + Mooncake，环境最重、最易失败，单独前置能在它挂时尽早暴露、并和全量单元测试的失败定位分开；环境变量开关让它在没有这些依赖的本地环境里被自动跳过，避免误报。

**练习 3**：`no-commit-to-branch` 钩子默认保护哪些分支？为什么需要它？

**参考答案**：默认保护 `main` 与 `master`，阻止开发者直推主干，强制走 PR 流程——这样合并前必然经过 lint 与 GPU CI 两道闸。

---

## 5. 综合实践

把本讲三块知识串起来：**假设你刚为 SpecForge 新增了一个算法 `mydraft`（按 u10-l2 的三件套：`AlgorithmSpec` 契约 + `providers` 端口 + `DraftTrainStrategy`），请列出你最少应当补齐的测试与门禁，并说明 overfit gate 替你验证了哪几类行为。**

**步骤 1 — 补单元测试（对照 4.1 的目录镜像规则）**：

| 测试目录 | 应补的测试 | 参考模板 | 验证什么 |
| --- | --- | --- | --- |
| `tests/test_algorithms/` | `test_mydraft_contracts.py` | `test_contracts.py` | `AlgorithmSpec` 是纯值（`_assert_pure_value`）、`FeatureContract` 的 `(mode,modality)` 键合法 |
| `tests/test_algorithms/` | `test_mydraft_registry.py`（或扩 `test_builtin_parity.py`） | `test_builtin_parity.py`、`test_registry.py` | `make_registration` 的 parity 对账（契约键 == provider 键、`capture_layout.output_names == storage.required_tensors`）、`registry.resolve("mydraft")` 能命中 |
| `tests/test_application/` | 扩 `test_composition.py` 加 `mydraft` 用例 | `test_composition.py` | `resolve_run` 能把 `strategy="mydraft"` 解析成正确 `AlgorithmRegistration`，拓扑校验通过 |
| `tests/test_runtime/` | `test_mydraft_strategy.py` | `test_peagle_strategy.py`、`test_compact_teacher_strategy.py` | `DraftTrainStrategy.forward_loss` 数值正确、`required_features` 与契约张量集对齐、`checkpoint_state_filter` 过滤正确 |
| `tests/test_utils/`（若自带 loss 算子） | `test_mydraft_loss.py` | `test_loss.py`、`test_dflash_losses.py` | 自定义 loss 算子的前向/反向数值 |

**步骤 2 — 跑通门禁**：

```bash
# 本地：pre-commit 过 + 相关单元测试过
pre-commit run --all-files
python -m unittest tests.test_algorithms.test_mydraft_contracts -v
python -m unittest tests.test_runtime.test_mydraft_strategy -v

# 端到端：复用 overfit gate（dry-run 先体检，再真跑）
GATE_DRY_RUN=1 CONFIG=<mydraft-disagg.yaml> TARGET_MODEL_PATH=... \
  DRAFT_CONFIG_PATH=<mydraft.json> SOURCE_DATA_PATH=... \
  bash scripts/gates/run_disaggregated_overfit_gate.sh
```

**步骤 3 — 说明 overfit gate 替你验证的几类行为**（这是本讲的落脚点）：

1. **数据契约**：`select_overfit_sample.py` 选出的单条样本符合 chat template 与 reasoning 策略、不截断、且 ≥ `2×block_size` 可训练 token。
2. **端到端运行时接线**：Mooncake + patched SGLang capture server + producer + consumer 全部正确起停、零拷贝传输通畅、无孤儿进程。
3. **训练数值正确性**：草稿能在 `MAX_STEPS`（默认 400）内把单条样本的 loss 压到 ≤ `1e-4`、token 精度到 `1.0`——这是「捕获特征正确 + forward/backward 数值正确」的强证据。
4. **检查点正确性**：最终的 `{run_id}-step{MAX_STEPS}/training_state.pt` 精确存在。
5. **导出与服务正确性**（`RUN_SERVING_GATE=true` 时）：`specforge export --to hf` 成功，真实 SGLang 服务返回的 `spec_accept_length ≥ block_size` 且生成内容吻合目标前缀——即训出来的草稿**真能加速推理**。
6. **清理正确性**：任意退出路径（EXIT/INT/TERM）都不留 GPU 孤儿。

> 待本地验证：真跑 overfit gate 需要 GPU、打过 patch 的 SGLang、Mooncake；没有这些环境时，至少应跑通 dry-run + 全部相关单元测试。

## 6. 本讲小结

- SpecForge 测试用**标准库 `unittest`**，由 `python -m unittest discover` 发现；`tests/test_*` 目录**镜像源码包**，改某源码文件就能立刻定位对应测试目录。
- 单元测试以「工厂函数造最小样本 → 断言不变量」为通用范式；`tests/utils.py` 复用生产代码 `launch_plan._terminate_processes` 做进程清理，避免 SGLang 孤儿。
- `scripts/gates/run_disaggregated_overfit_gate.sh` 是一条命令的**端到端 overfit + 服务门禁**：单样本过拟合到 loss≈0 来证明整条 disaggregated 链路数值正确，再链接真实服务验收加速。
- gate 的脚手架在 `_e2e_common.sh`：`gate_require_*` 守卫、`gate_start_service/stop_services` 进程组生命周期、`gate_install_cleanup_traps` 任意路径清理、`GATE_DRY_RUN` 零成本体检。
- `check_overfit_metrics.py` 是**方法无关**的叶子检查器，新算法直接传不同 `--max-loss`/`--min-accuracy` 即可复用。
- 两道质量闸：本地 `pre-commit`（autopep8/isort/black/ruff/clang-format + 一篮子 check 守卫含 `no-commit-to-branch`）自动修复；CI `lint.yaml`（同配置 `--all-files`）+ GPU `test.yaml`（实捕 gate + 全量 unittest）只验证。新算法至少补 `test_algorithms`（契约/注册）、`test_application`（组合根）、`test_runtime`（策略）三类测试。

## 7. 下一步学习建议

- **回看 u10-l1 / u10-l2**：本讲的「补测试目录」清单正是这两讲新增草稿架构 / 新增算法三件套的对应验证；建议把本讲的综合实践与那两讲的实践合成一个完整「新增算法 → 补测试 → 过 gate」的端到端练习。
- **精读 gate 叶子脚本**：通读 `scripts/gates/select_overfit_sample.py`、`run_dflash_chat_serving_gate.py`，理解「选样本」「验收服务」两个薄叶子的输入输出契约，日后可仿写 `mydraft` 专属的 serving gate。
- **读 CI 全貌**：浏览 `.github/workflows/publish_pypi.yaml` 与 `publish_docs.yaml`，理解发布门禁如何接力 test/lint；以及 `tests/ci/gpu_lock_exec.py` 如何在 self-hosted GPU 上串行化任务。
- **尝试写一个最小测试**：仿照 `tests/test_algorithms/test_contracts.py`，为你假想的 `mydraft` 写一个 `AlgorithmSpec` 纯值校验测试并跑 `python -m unittest`，把本讲从「读」推进到「写」。
