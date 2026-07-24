# 进程拓扑与多进程 Runner

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 SGLang-Omni 如何从一份声明式 `PipelineConfig` 求解出「哪些 stage 跑在同一个 OS 进程里」，以及为什么**每个非 TP 阶段都必须显式声明 `process`**。
- 区分两个常被混淆的概念：**进程共置**（多个 stage 共用一个 OS 进程）与 **GPU 共置**（多个 stage/进程共用一张 GPU）。这正是 `topology.py` 要讲清的唯一问题。
- 读懂 `StageLaunchConfig` / `StageWorkerProcessSpec` 这两份「可 pickle 的子进程规格」，理解为何所有函数都只能以点分路径（dotted path）传递、由子进程 `import_string` 再解析。
- 跟踪 `MultiProcessPipelineRunner.start()` 的完整生命周期：编译拓扑 → 分配端点 → spawn 子进程 → 等待 ready → 向 Coordinator 注册。
- 看懂子进程入口 `stage_process_main` 如何在不重新编译 pipeline 的前提下，仅凭规格重建出 `Stage` 与 scheduler。

## 2. 前置知识

本讲假定你已经读过：

- **u2-l5 声明式配置**：知道 `StageConfig` 的 `gpu`、`tp_size`、`process`、`fused_stages` 等字段，以及「静态全集 + 请求感知子集」的配置哲学。
- **u3-l1 Stage 抽象**：知道 Stage 是一个 IO 外壳，把所有计算 dispatch 给 scheduler，且不因 scheduler 类型而分支。

几个本讲会用到的术语，先用大白话解释：

- **进程拓扑（process topology）**：给定一组 stage，回答「它们被装进几个 OS 进程、每个进程装了谁」。它只关心进程边界，**不负责**把 stage 绑到哪张 GPU。
- **放置（placement）**：把 stage 绑到具体 GPU id，并给每张 GPU 算出显存预算。这是 `placement.py` 的活，是拓扑求解的**输入**而非输出。
- **union-find（并查集）**：一种把「有关系的元素」归到同一集合的经典算法。本讲用它把「声明了同一个 `process` 名」或「属于同一个 `fused_stages` 组」的 stage 合并进同一个进程组。
- **pickle**：Python 标准库的对象序列化协议。`multiprocessing` 用 `spawn` 方式拉起子进程时，父进程传给子进程的所有参数都必须能被 pickle。函数对象不可直接 pickle，所以这里只传函数的**点分路径字符串**。
- **TP（tensor parallel）/ rank**：张量并行把一个 stage 的模型切到多张 GPU 上，每张 GPU 跑一个 rank。本讲里 TP 阶段是「一进程一 rank」的特例。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sglang_omni/config/topology.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/topology.py) | 进程拓扑求解器。吃进放置计划，输出 `ProcessTopologyPlan`（进程组、stage→进程映射、TP 进程名）。**只回答「谁和谁共进程」**。 |
| [sglang_omni/pipeline/mp_runner.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/mp_runner.py) | `MultiProcessPipelineRunner`：拓扑的拥有者，负责把拓扑翻译成 `StageGroup`、spawn 子进程、监控、关停。 |
| [sglang_omni/pipeline/stage_workers.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py) | 子进程规格（`StageLaunchConfig`/`StageWorkerProcessSpec`）、生命周期管理（`StageGroup`）、子进程入口（`stage_process_main`）。 |
| [sglang_omni/config/placement.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/placement.py) | 放置计划 `StagePlacementPlan`，是拓扑求解的输入。 |
| [sglang_omni/pipeline/runtime_config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/runtime_config.py) | `prepare_pipeline_runtime`：把融合、放置、拓扑、端点分配串成一份 `PipelineRuntimePrep`。 |
| [sglang_omni/models/qwen3_omni/config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py) | Qwen3-Omni 的 stage 定义。本讲用它做「单进程」与「GPU 共置」两个真实例子。 |
| [tests/unit_test/pipeline/test_topology.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_topology.py) | 拓扑求解的单元测试，是理解边界规则最快的读物。 |

---

## 4. 核心概念与源码讲解

### 4.1 进程拓扑：求解「哪些 stage 共享一个 OS 进程」

#### 4.1.1 概念说明

把多个 stage 装进同一个 OS 进程，有两个潜在好处：省进程启动开销、省跨进程通信（同进程的 stage 之间可以走进程内直接派发，不必经 ZMQ）。但「哪些 stage 该共进程」并不能从 GPU 绑定直接推断——因为还允许「同 GPU 多进程」「多 GPU 单进程（受限）」等组合。于是 SGLang-Omni 把这个决定**显式化**：

- 非 TP 阶段**必须**在配置里声明 `process` 字段。声明了**同一个 `process` 名**的多个 stage，会被合并进同一个 OS 进程。
- 此外，`PipelineConfig.fused_stages` 提供「线性相邻 stage 强制共进程」的语法糖（见 4.1.3）。
- TP 阶段（`tp_size > 1`）走单独路径，每个 rank 独占一个 OS 进程，不参与上面的合并。

`topology.py` 的模块开关注释把这件事说得很直白：**它不决定 GPU 放置，只回答一个问题——哪些非 TP 阶段该跑在同一个 OS 进程里**。

[sglang_omni/config/topology.py:L1-L7](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/topology.py#L1-L7) —— 模块定位：吃进已求解的放置计划，只回答「哪些非 TP 阶段共进程」。

#### 4.1.2 核心流程

设非 TP 阶段集合为 \( S \)。定义一个等价关系 \(\sim\)：\(a \sim b\) 当且仅当

- \(a\) 与 \(b\) 声明了相同的 `process` 名；或
- \(a\) 与 \(b\) 同属某个 `fused_stages` 组。

对该等价关系取传递闭包，得到的每一个等价类（连通分量）就是一个进程组、对应一个 OS 进程。OS 进程总数为

\[
N_{\text{proc}} \;=\; \bigl| S \big/ \sim \bigr| \;+\; \sum_{s \in T} \text{tp\_size}(s)
\]

其中 \( T \) 是 TP 阶段集合，每个 TP 阶段贡献 `tp_size` 个进程。

求解用 **union-find（并查集）**：先把所有 stage 各自成单元素集合，再按「同 process 名」和「同 fused 组」两轮 `union`，最后按 `find` 根归并。流程式伪代码：

```
输入: stages（已融合）、gpu_placement
1. non_tp = [s for s in stages if s.tp_size == 1]
2. 校验每个 s in non_tp 都声明了 process        # 否则 ValueError
3. 并查集初始化：每个 stage 自成一组
4. for 每个 process 名 p:  union 同名 p 的所有 stage
5. for 每个 fused 组 g:    union g 中的非 TP stage
6. 按 find 根聚合 → 得到若干 component（进程组）
7. 给每个组算一个进程名（共享名优先，否则 fused_ 前缀）
8. 校验进程名唯一、组不跨多 GPU、显存预算不超限
```

注意第 5 步：`fused_stages` 即使现在只是一个声明（见 4.1.3），也仍然参与合并逻辑。

#### 4.1.3 源码精读

入口函数 `build_process_topology_plan` 接收已求解的放置计划，组装出 `ProcessTopologyPlan` 并跑两道校验。

[sglang_omni/config/topology.py:L36-L57](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/topology.py#L36-L57) —— 拓扑入口：先建非 TP 进程组，再建 TP 进程名，最后做唯一性与 GPU 共置校验。

核心是 `_build_process_groups`，它先筛出非 TP 阶段、强制要求它们都声明了 `process`：

[sglang_omni/config/topology.py:L60-L80](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/topology.py#L60-L80) —— 筛非 TP 阶段、校验 process、再调 `_resolve_non_tp_process_components` 求连通分量、命名。

「必须声明 process」这条硬规则在**两个**地方都做了兜底——schema 构造时一次，拓扑求解时一次：

[sglang_omni/config/schema.py:L399-L406](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L399-L406) —— schema 层校验：非 TP 阶段缺 `process` 直接报错。

[sglang_omni/config/topology.py:L83-L89](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/topology.py#L83-L89) —— 拓扑层二次校验同一规则。

真正的求解器是并查集 `_resolve_non_tp_process_components`。下面是它的关键骨架（删减后）：

[sglang_omni/config/topology.py:L92-L138](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/topology.py#L92-L138) —— 并查集：先按 `process` 名 `union`（L115-L122），再按 `fused_stages` 组 `union`（L123-L133，且只对组内的非 TP 阶段），最后按 `find` 根归并成进程组。

```python
# 第一轮：同名 process 合并
by_process: OrderedDict[str, list[str]] = OrderedDict()
for stage in stages:
    by_process.setdefault(stage.process or "", []).append(stage.name)
for stage_names in by_process.values():
    first = stage_names[0]
    for stage_name in stage_names[1:]:
        union(first, stage_name)

# 第二轮：fused_stages 组合并（仅非 TP）
for group in config.fused_stages or []:
    local_stage_names = [n for n in group
                         if n in stage_by_name and stage_by_name[n].tp_size == 1]
    if not local_stage_names:
        continue
    first = local_stage_names[0]
    for stage_name in local_stage_names[1:]:
        union(first, stage_name)
```

进程命名规则在 `_component_process_name`：若组内所有 stage 共享同一个显式 `process` 名，就用它；否则（来自不同 process 名、靠 `fused_stages` 合到一起）用 `fused_<s1>_<s2>` 前缀，并对重名加 `_N` 后缀。

[sglang_omni/config/topology.py:L141-L157](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/topology.py#L141-L157) —— 进程组命名：共享名优先，融合组用 `fused_` 前缀。

> **关于 `fused_stages` 的现状（重要，别被名字骗了）**：`PipelineConfig.apply_fusion()` 目前是一个**空操作**——它原样返回 stage 列表，并不把 fused 组合并成单个 stage。

[sglang_omni/config/schema.py:L470-L472](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L470-L472) —— `apply_fusion` 当前为 no-op：只返回原 stage 列表与恒等 name_map。

也就是说，「融合」目前的效果**仅限于让这些 stage 共进程**（拓扑层生效），并不会在配置层把它们重写成一个 stage。schema 仍会对 `fused_stages` 做严格契约校验（必须相邻、必须线性、必须同 GPU、不含 TP/内部 fan-in）：

[sglang_omni/config/schema.py:L408-L468](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L408-L468) —— `_validate_fusion` / `_validate_fused_group_contract`：fused 组必须相邻有序、线性、单 GPU、不含 TP 与内部 fan-in。

#### 4.1.4 代码实践

**目标**：用拓扑的单元测试直观验证「同 process 名 → 共进程」「同 process 跨 GPU → 报错」两条规则。

**步骤**：

1. 打开 [tests/unit_test/pipeline/test_topology.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_topology.py)。
2. 阅读 `test_same_process_same_gpu_does_not_require_memory_budgets`（L114-L127）：两个 stage 同 `process="p0"` 且同 `gpu=0`，断言拓扑产出一个组 `("p0", ("a","b"), 0)`。
3. 阅读 `test_one_process_group_cannot_span_multiple_gpus`（L177-L188）：同 `process="p0"` 但一个 `gpu=0`、一个 `gpu=1`，断言抛 `spans multiple GPUs`。
4. 在仓库根目录运行（**待本地验证**，需要已按 u1-l2 装好依赖）：

```bash
pytest tests/unit_test/pipeline/test_topology.py -v
```

**需要观察的现象**：第 2 个用例能产出单一进程组且不要求显存预算；第 3 个用例在校验阶段失败。

**预期结果**：前者 `topology.groups == [("p0",("a","b"),0)]`；后者抛出含 `spans multiple GPUs` 的 `ValueError`。如果跑不起来，这是「源码阅读型实践」——直接对照断言理解行为即可。

#### 4.1.5 小练习与答案

**练习 1**：三个 stage `a/b/c` 都在 `gpu=0`，`a` 和 `b` 声明 `process="p0"`，`c` 声明 `process="p1"`。会得到几个进程组？是否需要给每个 stage 写显存预算？

> **答案**：两个进程组 `p0=(a,b)`、`p1=(c)`。因为现在一张 GPU 上有两个进程，需要显存预算——参考测试 `test_same_gpu_multiple_processes_rejects_missing_budget`（L150-L161），缺预算会被拒。同一 `process` 名的 `a/b` 共进程时**不**需要单独预算（见 `test_same_process_same_gpu_does_not_require_memory_budgets`）。

**练习 2**：`fused_stages=[["x","y"]]` 把两个 stage 合到一组，但 `apply_fusion` 是 no-op。那么「融合」当前到底改变了什么？

> **答案**：当前只在**进程拓扑层**生效——让 `x`、`y` 共进程（并查集第二轮 union）。它不会在配置层把 `x`、`y` 重写成单个 stage，`stages` 列表长度不变。

---

### 4.2 StageLaunchConfig 与 StageWorkerProcessSpec：可 pickle 的子进程规格

#### 4.2.1 概念说明

`MultiProcessPipelineRunner` 用 Python `multiprocessing` 的 **spawn** 方式拉起子进程。spawn 的子进程是一个全新的解释器，父进程必须把所有「启动所需的参数」**序列化（pickle）**后传过去。这里有两层结构：

- **`StageLaunchConfig`**：一个**逻辑 stage 实例**的完整启动元数据。注意是「实例」而非「stage 类型」——同一个 stage 在 TP 下会派生出多个实例（leader/follower 各一份）。
- **`StageWorkerProcessSpec`**：一个 **OS 进程**要跑的全部内容 = 进程名 + 一个或多个 `StageLaunchConfig`。共进程的多个非 TP stage 共享一个 spec；TP 每个 rank 独占一个 spec。

最关键的设计约束写在类文档里：**所有字符串引用（factory、merge_fn 等）都是点分导入路径，由子进程用 `import_string` 解析**。原因是函数对象不能可靠 pickle，而字符串可以。

#### 4.2.2 核心流程

```
父进程:
  对每个 stage:
    - tp_size==1  → 生成 1 个 StageLaunchConfig(role="single")
    - tp_size>1   → 生成 tp_size 个 Config(role="leader"/"follower"),
                    每个 rank 一份, 共享 follower_* 队列
  按进程组打包:
    - 非 TP 组 → 1 个 StageWorkerProcessSpec(stage_specs=[该组所有 single])
    - TP 组    → tp_size 个 Spec, 每个 Spec 只装 1 个 Config
子进程(spawn):
  收到 StageWorkerProcessSpec → 对每个 StageLaunchConfig:
    import_string(factory) → 构造 scheduler → 构造 Stage
```

`StageLaunchConfig` 里有一个贯穿全类的角色三态：`role ∈ {"single","leader","follower"}`，由属性 `owns_external_io` 统一区分——只有 `single`/`leader` 对外收发 ZMQ，follower 只经内部队列与 leader 通信。

#### 4.2.3 源码精读

`StageLaunchConfig` 的字段分若干组（身份/工厂/路由/扇入/通信/端点/流式/TP 内部控制）。最值得记住的是这几个：

[sglang_omni/pipeline/stage_workers.py:L35-L60](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L35-L60) —— `StageLaunchConfig` 类文档与身份/工厂字段：明确「这是逻辑实例的元数据，一个 worker 进程可携带多份（共置非 TP stage）」，且所有函数引用都是点分路径。

角色三态由属性统一判定：

[sglang_omni/pipeline/stage_workers.py:L108-L119](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L108-L119) —— `owns_external_io`：只有 `single`/`leader` 拥有对外 IO；这是后续「Coordinator 只和 rank0 通信」的根。

`StageWorkerProcessSpec` 极简——就是一个 OS 进程的载荷：

[sglang_omni/pipeline/stage_workers.py:L121-L127](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L121-L127) —— `StageWorkerProcessSpec = 进程名 + 一组 StageLaunchConfig`。

这两层 spec 是在 `mp_runner.py` 的 `_build_stage_groups` 里填充的。TP 与非 TP 走不同分支，但最终都汇成 `StageGroup` 列表：

[sglang_omni/pipeline/mp_runner.py:L87-L182](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/mp_runner.py#L87-L182) —— 构建循环：`tp_size==1` 走 `_build_single_stage_spec`，否则走 `_build_tp_stage_specs`；最后非 TP 进程组的多个 single spec 被装进**同一个** `StageWorkerProcessSpec`（L165-L179）。

非 TP 进程组的打包代码，直接对应「共进程 = 共 spec」：

```python
groups: list[StageGroup] = []
for group in process_plan.groups:
    groups.append(
        StageGroup(
            group.name,
            [
                StageWorkerProcessSpec(
                    process_name=group.name,
                    stage_specs=[
                        single_stage_specs[stage_name]
                        for stage_name in group.stage_names
                    ],
                )
            ],
        )
    )
```

[sglang_omni/pipeline/mp_runner.py:L164-L180](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/mp_runner.py#L164-L180) —— 非TP进程组打包：一组多 stage → 一个 Spec；TP 组则 extend 到列表后。

#### 4.2.4 代码实践

**目标**：理解「共进程多 stage 共享一个 Spec」与「TP 每 rank 一个 Spec」的差别。

**步骤**（源码阅读型）：

1. 在 [mp_runner.py:L153-L162](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/mp_runner.py#L153-L162) 看 TP 分支：每个 rank 的 `StageLaunchConfig` 各自包成一个 `StageWorkerProcessSpec`，再组成一个 `StageGroup`。
2. 对比上一段非 TP 分支（L164-L180）：一个进程组的多个 stage 共享同一个 Spec。
3. 思考：如果一个 TP stage 被错误地塞进一个含别的 stage 的 Spec，会怎样？

**需要观察的现象**：TP stage 的 Spec 永远只含 1 个 `stage_specs`。

**预期结果**：见 `_get_worker_process_env` 的硬断言——TP stage 必须独占进程，混入其它 stage 会直接 `AssertionError`。

[sglang_omni/pipeline/stage_workers.py:L129-L146](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L129-L146) —— 硬不变量：TP stage 必须独占 OS 进程，混入其它 stage 即断言失败。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `StageLaunchConfig.factory` 存的是字符串 `"xxx.create_decode_executor"` 而不是函数对象本身？

> **答案**：spawn 子进程时父进程要 pickle 全部参数，函数对象（尤其闭包/lambda）不可靠 pickle。存点分路径，子进程用 `import_string` 自己 import，既可 pickle 又延迟到子进程才加载重型模块。

**练习 2**：一个共置进程组里有 3 个非 TP stage，会生成几个 `StageLaunchConfig`、几个 `StageWorkerProcessSpec`？

> **答案**：3 个 `StageLaunchConfig`（每个 stage 一份，role 都是 `single`），但只有 **1 个** `StageWorkerProcessSpec`（`stage_specs` 装这 3 份）。

---

### 4.3 MultiProcessPipelineRunner：拓扑的拥有者与启动生命周期

#### 4.3.1 概念说明

`MultiProcessPipelineRunner`（以下简称 Runner）是「唯一的服务主路径」拥有者。它的注释明确写出它能撑起三种拓扑：**一个进程装多个非 TP stage**、**一张 GPU 上多个进程**、以及上游既有的**一进程一 rank 的 TP**。

它只做编排，不做计算：把配置编译成运行时准备态、spawn 子进程、等它们 ready、把各 stage 的控制端点注册给 Coordinator，然后靠一个后台监控协程看护子进程存活。Runner 自己不碰模型前向——那是子进程里 scheduler/Stage 的事（承接 u3-l1）。

#### 4.3.2 核心流程

`start()` 的时序可拆成 7 步：

```
start(timeout):
  1. ctx = spawn 上下文
  2. prep = prepare_pipeline_runtime(config)   # 融合+放置+拓扑+端点+IPC目录
  3. groups = _build_stage_groups(...)          # 拓扑 → StageGroup 列表
  4. 建 Coordinator, coordinator.start(), 跑 completion 循环
  5. for g in groups: g.spawn(ctx)              # 真正 fork/exec 子进程
  6. await gather(g.wait_ready(timeout))         # 等所有子进程就绪
  7. 检查启动期死亡; 把端点 register_stage 给 Coordinator; 启动 _monitor_children
```

关停 `stop()` 的顺序相反：先经 Coordinator 给各 stage 发 shutdown，再 `group.shutdown()` join 子进程，最后取消 completion 任务、关 Coordinator、清 IPC 目录。

#### 4.3.3 源码精读

`start()` 全貌：

[sglang_omni/pipeline/mp_runner.py:L386-L462](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/mp_runner.py#L386-L462) —— `start()`：编译运行时 → 建 groups → spawn → wait_ready → 死亡检查 → 注册 Coordinator → 启动监控；失败时走 `_cleanup_on_failure`。

关键是第 2 步 `prepare_pipeline_runtime`，它把四件事缝成一份 `PipelineRuntimePrep`：融合 stage、放置计划、**进程拓扑**、端点分配。Runner 拿到的 `prep.process_plan` 就是 4.1 求解的产物。

[sglang_omni/pipeline/runtime_config.py:L92-L131](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/runtime_config.py#L92-L131) —— `prepare_pipeline_runtime`：`apply_fusion` → `build_stage_placement_plan` → `build_process_topology_plan` → `allocate_endpoints`，任一步失败都会回滚自建的 IPC 目录。

`PipelineRuntimePrep` 就是 Runner 启动所需的全套状态：

[sglang_omni/pipeline/runtime_config.py:L54-L65](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/runtime_config.py#L54-L65) —— `PipelineRuntimePrep` 字段：fused 后的 stages、name_map、entry_stage、endpoints、placement_plan、process_plan、IPC 目录。

子进程就绪后，Runner 把每个 group 暴露的「拥有对外 IO 的 stage 端点」注册给 Coordinator——注意只注册 `single`/`leader`，follower 不注册：

[sglang_omni/pipeline/mp_runner.py:L443-L458](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/mp_runner.py#L443-L458) —— 注册端点 + 启动监控 + 打印 `N stage(s), M process(es)` 汇总。

监控协程每 5 秒巡检一次，任一子进程死亡就把错误塞给 Coordinator、`fail_pending_requests`、再 `stop()` 整个 runtime：

[sglang_omni/pipeline/mp_runner.py:L464-L482](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/mp_runner.py#L464-L482) —— `_monitor_children` + `_fail_runtime`：检测到死亡进程即 fail-fast，把在途请求全部标记失败。

`StageGroup.stage_control_endpoints` 正是「只暴露 owns_external_io 的 spec」：

[sglang_omni/pipeline/stage_workers.py:L235-L241](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L235-L241) —— 只把 `single`/`leader` 的 recv 端点对外暴露，follower 对 Coordinator 不可见。

#### 4.3.4 代码实践

**目标**：通过日志验证「Runner 启动后 stage 数 ≠ 进程数」这一拓扑效果。

**步骤**：

1. 阅读 [mp_runner.py:L450-L458](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/mp_runner.py#L450-L458) 那条 `MultiProcessPipelineRunner started: %d stage(s), %d process(es)` 日志的来源：`total_stages` 数的是 `stage_control_endpoints`（拥有对外 IO 的 stage），`total_procs` 数的是 `process_count`。
2. 假设一个共置 text-only Qwen3-Omni（6 stage 全 `process="pipeline"`）启动成功，预测这条日志会打印什么。

**需要观察的现象**：stage 数与进程数的关系取决于拓扑——共进程时进程数 < stage 数。

**预期结果**（待本地验证）：对 6 stage 单进程配置，日志应为 `6 stage(s), 1 process(es)`。对 8 stage 的 speech colocated（每 stage 独立 process）则约为 `8 stage(s), 8 process(es)`。

#### 4.3.5 小练习与答案

**练习 1**：Runner 的 `start()` 在哪一步会触发进程拓扑求解？求解发生在父进程还是子进程？

> **答案**：在 `prepare_pipeline_runtime`（`start()` 第 2 步）里调用 `build_process_topology_plan`。求解完全在**父进程**完成，子进程只收到求解结果（`StageWorkerProcessSpec`）。

**练习 2**：`stage_control_endpoints` 为什么不包含 follower？

> **答案**：follower 的 `role="follower"`，`owns_external_io` 为假。TP 组里只有 rank0（leader）收 ZMQ、与 Coordinator 对话，follower 经内部队列与 leader 通信（承接 u2-l4「TP 阶段只与 rank0 通信」）。

---

### 4.4 子进程重建：stage_process_main → factory → Stage

#### 4.4.1 概念说明

子进程入口 `stage_process_main` 是 spawn 的 target。它的职责很纯粹：**只凭一份 `StageWorkerProcessSpec`，重建出 Stage 并跑起来**，绝不重新解析 `PipelineConfig`、不重新编译 pipeline。这正是「在子进程中重建 Stage 而不重编译 pipeline」的含义——父进程已经把拓扑、端点、放置都算好并序列化进 spec 了。

共进程的多 stage 在这里有一个重要的失败语义：它们共享同一个 asyncio 事件循环、`asyncio.gather` 并发跑，**任一 stage 抛异常，整个进程退出**，进程内没有 per-stage 故障隔离。监控协程会据此 fail-fast 所有在途请求。

#### 4.4.2 核心流程

```
stage_process_main(spec, ready_event, error_channel):
  for stage_spec in spec.stage_specs:
      _prepare_cuda_environment(stage_spec)        # TP rank 映射到单可见 GPU
  apply_gpu_compat_env_defaults()
  _run_process(spec, ready_event):
      local_dispatcher = LocalStageDispatcher()
      for stage_spec in spec.stage_specs:
          stages.append(_construct_stage(stage_spec))   # import factory → scheduler → Stage
      local_dispatcher.register_many(stages)            # 同进程 stage 注册进程内派发
      asyncio.run(_start_and_run):                      # 一个循环跑所有 stage
          await stage.start() for all; ready_event.set()
          gather(stage.run() for all)
```

两个细节值得注意：

- **scheduler 构造按 GPU 串行**：`_construct_scheduler` 用 `gpu_startup_lock(gpu_id)` 保证同一 GPU 上多个 stage 冷启动不会并发抢显存/编译资源，冷启动时间从 `max` 退化成 `sum`。
- **同进程派发优化**：`_resolve_same_process_targets`（父进程算好，塞进 spec 的 `same_process_targets`）告诉 Stage「哪些下游 stage 与我同进程」，于是这些边走 `LocalStageDispatcher` 进程内直传，而非 ZMQ+relay。这是共进程的**性能回报**，也预告了 u6-l1 的通信路由。

#### 4.4.3 源码精读

子进程入口与失败清理：

[sglang_omni/pipeline/stage_workers.py:L365-L406](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L365-L406) —— `stage_process_main`：准备 CUDA 环境、`_run_process`；失败时销毁 torch.distributed 进程组、回收 CUDA 显存、把 traceback 塞进 error_channel 让父进程看到。

`_run_process` 的文档把共进程语义讲得很清楚：

[sglang_omni/pipeline/stage_workers.py:L409-L427](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L409-L427) —— 共进程多 stage 共享一个 asyncio 循环；任一 stage 抛错则整进程退出，进程内无 per-stage 隔离；scheduler 构造按 GPU 串行。

`_start_and_run` 内核：先全部 `stage.start()` 再 `ready_event.set()`，再 `gather` 所有 `stage.run()`：

[sglang_omni/pipeline/stage_workers.py:L431-L448](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L431-L448) —— 全部 stage `start()` 完才置 ready；`asyncio.gather` 并发驱动所有 stage 的 `run()`。

工厂构造与 GPU 串行：

[sglang_omni/pipeline/stage_workers.py:L772-L790](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L772-L790) —— `_construct_scheduler`：`import_string(factory)` 再按签名注入参数；有 GPU 时用 `gpu_startup_lock` 串行化。

同进程派发的求解（父进程侧，结果塞进 spec）：

[sglang_omni/pipeline/mp_runner.py:L185-L212](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/mp_runner.py#L185-L212) —— `_resolve_same_process_targets`：枚举 `next`/`stream_to` 目标，凡目标与本 stage 同进程且非 TP，即记为「可进程内直传」。

#### 4.4.4 代码实践

**目标**：跟踪一个 TP leader/follower 的环境差异，理解「每个 rank 独占一个可见 GPU」。

**步骤**（源码阅读型）：

1. 读 [stage_workers.py:L793-L820](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L793-L820) 的 `get_stage_process_env`：TP stage 会把 `CUDA_VISIBLE_DEVICES` 重映射为该 rank 对应的那一张物理 GPU，并设 `SGLANG_ONE_VISIBLE_DEVICE_PER_PROCESS=true`。
2. 读 [stage_workers.py:L823-L863](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L823-L863) 的 `_prepare_cuda_environment` + `_normalize_spec_gpu_id_to_local_device`：子进程把 `gpu_id` 归一化成本地设备 0。

**需要观察的现象**：每个 TP rank 子进程启动时只「看见」一张 GPU，`gpu_id` 在 spec 里被改写成 0。

**预期结果**：日志出现 `TP stage ... rank N sees CUDA_VISIBLE_DEVICES=... (local gpu_id=0)`。这就是「TP 阶段一进程一 rank、独占 GPU」的物理实现，也是 4.2 那条「TP 必须独占进程」断言的理由。

#### 4.4.5 小练习与答案

**练习 1**：共进程的 3 个 stage，其中一个在 `run()` 里抛了未捕获异常，另外两个会怎样？

> **答案**：三者共享一个 asyncio 循环、用 `asyncio.gather` 驱动，任一抛错会让整个进程退出（`_run_process` 无 per-stage 隔离）。父进程的 `_monitor_children` 检测到死亡后 `_fail_runtime`，把所有在途请求标失败并 `stop()`。

**练习 2**：`_resolve_same_process_targets` 的存在让共进程除了省进程数之外，还有什么实际收益？

> **答案**：让同进程的相邻 stage 边走 `LocalStageDispatcher` 进程内直接派发，跳过 ZMQ 控制平面与 relay 数据平面，降低延迟与序列化开销。这正是「进程共置」相对「同 GPU 多进程」的性能回报（详情见 u6-l1 通信路由）。

---

## 5. 综合实践

**任务**：在 `topology.py` / `mp_runner.py` 中定位「哪些 stage 共享一个 OS 进程」的求解逻辑，并用真实配置解释 Qwen3-Omni 如何把多阶段压进进程。

**第一步——定位求解逻辑**：进程共置的求解在 [`_resolve_non_tp_process_components`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/topology.py#L92-L138)（topology.py:L92-L138），它是一个并查集：第一轮按 `process` 名 `union`，第二轮按 `fused_stages` 组 `union`（仅非 TP），最后按 `find` 根归并成进程组。每个等价类 = 一个 OS 进程。TP 阶段不参与，由 [`_build_tp_process_names`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/topology.py#L160-L172) 给每个 rank 派生独立进程名。

**第二步——读真实配置**（**注意区分两个易混的概念**）：

1. **真正的「单进程」例子 = text-only Qwen3-Omni**。看 [`_text_stages`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L212-L220)（config.py:L212-L220）：`preprocessing/image_encoder/audio_encoder/mm_aggregate/thinker/decode` 全部 `process="pipeline"`。并查集第一轮把它们全部 `union` 成一个等价类 → 1 个进程组 → 1 个 OS 进程装 6 个 stage。这就是「把多阶段压进单进程」。

2. **「colocated」≠「单进程」**。看 [`Qwen3OmniSpeechColocatedPipelineConfig`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L352-L373) 的 docstring 与 [`_SPEECH_DEFAULT_PROCESSES`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L260-L269)：8 个 speech stage 各自有**独立**的 `process` 名。并查集不会合并它们 → **8 个 OS 进程**。这里的「colocated」指 **GPU 共置**——`image_encoder/audio_encoder/thinker/talker_ar/code2wav` 都绑到 `gpu=0`（见 [config.py:L366-L373](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L366-L373)），由各 stage 的 `total_gpu_memory_fraction` 显存预算控制在同一张卡上共存（参考 [qwen3_omni_colocated_h100_bf16.yaml](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/examples/configs/qwen3_omni_colocated_h100_bf16.yaml) 的 0.02+0.02+0.78+0.10+0.02）。

**第三步——写结论**：用一段话说明——

> SGLang-Omni 把「进程共置」与「GPU 共置」拆成两个正交维度。进程边界由并查集按 `process` 名与 `fused_stages` 求解（`topology.py`）；GPU 边界由放置计划与显存预算求解（`placement.py`，是拓扑的输入）。text-only Qwen3-Omni 令全部 6 个 stage 共享 `process="pipeline"`，把多阶段真正压进**单进程**；而 speech colocated 配置令 8 个 stage 各持独立 process，只是把 5 个 GPU-resident stage 通过显存预算压到**同一张 GPU**——它是「8 进程 / 1 GPU」，不是「1 进程」。`topology.py` 的价值正是让这两件事都不再含糊。

**验证**（待本地验证，需装好依赖与权重）：

```bash
# 单进程 text 变体：启动日志应显示 6 stage(s), 1 process(es)
sgl-omni serve --model-path Qwen/Qwen3-Omni-30B-A3B-Instruct --text-only --log-level info

# 对照单元测试中「同 process 跨 GPU 报错」「同 GPU 多进程需预算」两条规则
pytest tests/unit_test/pipeline/test_topology.py -v
```

如果无法在本机起服务，本任务可作为纯源码阅读实践完成——结论可直接从 `_text_stages`、`_SPEECH_DEFAULT_PROCESSES` 与并查集逻辑推导得出。

## 6. 本讲小结

- **进程拓扑只回答一个问题**：哪些非 TP 阶段共进程。它**不**决定 GPU 放置，放置计划（`placement.py`）是它的输入。
- **两条合并规则**：声明相同 `process` 名、或同属一个 `fused_stages` 组——经并查集求传递闭包，每个等价类 = 一个 OS 进程。
- **硬规则**：每个非 TP 阶段必须显式声明 `process`；TP 阶段一进程一 rank、独占进程、不参与合并。
- **现状提醒**：`apply_fusion` 当前是 no-op，`fused_stages` 目前只在拓扑层让 stage 共进程，不在配置层重写 stage。
- **可 pickle 的规格**：`StageLaunchConfig`（逻辑实例）→ `StageWorkerProcessSpec`（OS 进程载荷），所有函数只以点分路径传递，子进程 `import_string` 再解析。
- **生命周期**：`MultiProcessPipelineRunner.start()` 在父进程求解拓扑 → spawn → wait_ready → 注册 Coordinator → 监控；子进程 `stage_process_main` 仅凭 spec 重建 Stage，共进程多 stage 共享一个 asyncio 循环、无 per-stage 故障隔离。

## 7. 下一步学习建议

- **u4-l1 调度器接口与 SimpleScheduler**：子进程里 Stage 桥接的 scheduler 长什么样，inbox/outbox 如何驱动计算。
- **u6-l1 通信路由与传输选择**：本讲提到的 `same_process_targets` / `LocalStageDispatcher` 如何与 `CommRouter` 配合，在同进程、同节点跨 GPU、跨节点三种边上选不同传输。
- **u6-l6 张量并行与流水线并行**：本讲的 TP 进程派生、NCCL 端口分配（`_NcclPortAllocator`）、rank0 IO 的完整图景。
- 继续阅读 [`tests/unit_test/pipeline/test_topology.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_topology.py) 的全部用例，把进程名唯一性、GPU 跨界、显存预算等边界规则一次性看透。
