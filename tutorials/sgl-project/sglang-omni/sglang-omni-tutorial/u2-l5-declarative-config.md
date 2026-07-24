# 声明式配置 PipelineConfig 与 StageConfig

## 1. 本讲目标

上一讲（u1-l5）我们学会了「查看、导出、局部覆盖」配置——把 `config view / config export` 当成一面镜子，照出某个模型管线会长什么样。本讲我们要走进镜子，看清楚那张被导出、被覆盖的表本身：`PipelineConfig` 与 `StageConfig` 到底声明了哪些字段、这些字段如何被运行时「派生」成拓扑、放置、终态。

学完本讲，你应当能够：

- 说出 `StageConfig` 每一类关键字段（`factory` / `next` / `terminal` / `route_fn` / `gpu` / `process` / `wait_for` / `merge_fn` / `stream_to` / `project_payload`）的职责；
- 区分「静态路由」与「请求感知路由」——`next` 是静态拓扑声明，`route_fn` / `wait_for_fn` / `terminal_stages_fn` 是按请求内容动态决定子集；
- 看懂 `CommConfig` 只「调参」不「选传输」的边界；
- 解释 `resolved_entry_stage` / `terminal_stages` / `gpu_placement` 这些派生字段为什么「不该手填、而该由 stage 派生」。

## 2. 前置知识

本讲默认你已经掌握 u1-l5 的结论：配置是「模型定义（提供拓扑）」与「模型无关运行时（Scheduler / ModelRunner）」之间的**契约**；模型家族决定拓扑、配置决定该拓扑在本机的运行方式。我们也承接 u2-l1 的分层视图（HTTP→Client→Coordinator→Stage→Scheduler→ModelRunner），以及 u2-l4 里 Coordinator「合并多终态」的概念——本讲会讲清楚那个「多终态」的静态期望集合 `terminal_stages` 从哪里来。

下面用三句话建立直觉：

1. **声明式（declarative）**：配置层只「声明」管线的形状（有几个阶段、谁连谁、谁终态、放哪块卡），**不写**请求时刻的行为。请求时刻的行为属于 stage / scheduler / model runner。
2. **派生（derived）**：入口、终态、GPU 放置这些「结论」是从 stage 列表**算出来**的，不是手填的——这样配置永远自洽，不会出现「声明了终态但 stage 漏标」的矛盾。
3. **静态超集 + 请求感知窄化**：`next` / `wait_for` / `stream_to` 永远写成「可能的全集」，而 `route_fn` / `wait_for_fn` / `stream_done_to_fn` 只在运行时挑出本次请求真正需要的子集。这是 SGLang-Omni 配置的核心设计范式。

> 术语：本讲反复出现「dotted import path（点号导入路径）」一词，指形如 `sglang_omni.models.qwen3_omni.merge.merge_for_thinker` 的字符串。配置里存的是字符串、不是函数对象，这样配置可以被序列化成 YAML、跨进程 pickle，等到运行时再去 import 真正的函数（见 u1-l5 的 `config export` 回读机制）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sglang_omni/config/schema.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py) | 配置「宪法」：`StageConfig` / `PipelineConfig` / `CommConfig` 及派生属性、校验逻辑全部在这里。 |
| [docs/developer_reference/config.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md) | 官方字段参考手册（字段表 + 路由规则 + 融合 + 张量并行）。 |
| [sglang_omni/models/qwen3_omni/config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py) | 一个真实模型家族如何**用** `StageConfig` 拼出整条管线（fan-out / fan-in / stream / terminal 全都出现）。 |
| [sglang_omni/models/qwen3_omni/request_builders.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py) | `route_fn` / `wait_for_fn` / `terminal_stages_fn` 这些「请求感知」函数的真实实现，看它们如何按 `output_modalities` 决定子集。 |
| [examples/configs/qwen3_omni_colocated_h100_bf16.yaml](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/examples/configs/qwen3_omni_colocated_h100_bf16.yaml) | 一份「紧凑覆盖文件」：只继承拓扑、只覆盖 `stage_overrides`，是本讲综合实践的素材。 |

## 4. 核心概念与源码讲解

### 4.1 StageConfig：一个阶段的全部声明

#### 4.1.1 概念说明

`StageConfig` 描述**一个逻辑阶段**：怎么构造它（`factory`）、它跑在哪（`gpu` / `process`）、它的常规结果去哪（`next` 或 `terminal`）、它是否参与 fan-in（`wait_for` / `merge_fn`）、它是否往下游流式吐块（`stream_to`）。一个 `PipelineConfig` 就是若干 `StageConfig` 的有序列表。

注意「逻辑阶段」这个词：它不等于「OS 进程」。多个逻辑阶段可以共享一个进程（靠 `process` 同名，见 u3-l4），一个逻辑阶段也可以展开成多个进程（靠 `tp_size`，见 4.1.2）。配置层只声明逻辑拓扑，进程拓扑是后面派生的。

#### 4.1.2 核心流程

`StageConfig` 的字段可以分成 6 组，记住这 6 组就掌握了全貌：

1. **身份与构造**：`name`（唯一名）、`factory`（点号路径）、`factory_args`（给工厂的显式参数）。
2. **普通路由**：`next`（静态下游，`str`/`list`/`None`）、`terminal`（是否终态）、`route_fn`（请求感知路由）。
3. **GPU 与并行**：`gpu`（卡号 / 卡号列表）、`tp_size`（张量并行度）、`parallelism.tp`、`process`（进程组标识）。
4. **运行时意图**：`runtime`（显存预算、`max_seq_len`、SGLang server args）、`runtime_arg_map`。
5. **fan-in**：`wait_for`（上游全集）、`wait_for_fn`（请求感知窄化）、`merge_fn`（合并函数，**必填**当设了 `wait_for`）。
6. **流式与投影**：`stream_to`（流式目标全集）、`stream_done_to_fn`、`project_payload`（按目标投影负载）、`comm`（通信调参）。

放置语义的要点（来自 [config.md:Tensor Parallelism](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md)）：

- `gpu=None` 表示 CPU 放置；`gpu=int` 单卡；`gpu=[...]` 列表且长度必须等于 `tp_size`。
- 每个**非 TP** 阶段**必须显式声明** `process`，没有隐式默认值——多个非 TP 阶段共享同一 `process` 值即共享一个 OS 进程。
- `tp_size>1` 时，运行时为每个 rank 派生一个进程，`process` 作为前缀生成 `{process}_tp{rank}`（未设则用 `name`）。

#### 4.1.3 源码精读

字段定义集中在 `StageConfig` 类的主体，注释把它们按组归类：

[sglang_omni/config/schema.py:147-186](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L147-L186) — 这段定义了上面 6 组字段的全部声明：从 `name`/`factory` 到路由、GPU、fan-in、流式、投影、`comm`。**路由不变量**就在这里以类型表达：`next` 与 `terminal` 是「二选一」，`route_fn` 只对设了 `next` 的阶段有意义（见 4.4 的校验）。

`factory_args` 的纪律尤其重要——文档明确：`gpu_id` 归放置规划管，**写在 `factory_args` 里会被拒绝**，要设设备就用 `gpu` 字段：

[docs/developer_reference/config.md:76](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L76) — 解释 `factory_args` 只放「显式参数」，而 `model_path` / `gpu_id` / `total_gpu_memory_fraction` 这类签名相关默认值由 worker 在 import 工厂之后注入。

真实模型怎么用这些字段？看 Qwen3-Omni 的预处理阶段——它是一次典型的 **fan-out**：

[sglang_omni/models/qwen3_omni/config.py:35-58](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L35-L58) — `next=["image_encoder","audio_encoder","mm_aggregate"]` 声明静态三路扇出全集；`route_fn=...resolve_preprocessing_next_stages` 在运行时按「这次请求到底有没有图/音」缩成真正需要的那几路；`project_payload` 给每个下游配一个投影函数（比如投影给 `image_encoder` 时只挑图像相关字段）。注意 `process="pipeline"`（文本管线里所有阶段共享一个进程）和 `factory_args={"thinker_max_seq_len": 8192}` 的写法。

再看一个**终态**阶段的极简写法：

[sglang_omni/models/qwen3_omni/config.py:155-162](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L155-L162) — `decode` 阶段 `terminal=True` 且 `can_accept_stream_before_payload=True`：它是终态（结果回传 Coordinator），同时还能在「正式负载到达前」先接收 thinker 流过来的 token（流式边细节见 4.3）。

#### 4.1.4 代码实践

**目标**：亲手触发 `StageConfig` 的「二选一」路由不变量，体会校验在构造时就生效。

**操作步骤**（在 u1-l2 装好的容器里启动 `python`）：

```python
# 示例代码：直接构造 schema，观察校验
from sglang_omni.config.schema import StageConfig

# 1) 正确写法：只设 next
ok = StageConfig(name="a", process="a", factory="x.create_a", next="b")
print("ok.terminal =", ok.terminal)

# 2) 错误写法：同时设了 next 和 terminal（构造阶段不会拦，管线级校验才会拦）
#    这里仅演示字段共存；真正报错要放进 PipelineConfig._validate_general
```

**需要观察的现象**：第 1 步正常构造；`ok.terminal` 为 `False`。

**预期结果**：「二选一」校验（`must set exactly one of 'next' or 'terminal'`）发生在 `PipelineConfig._validate_general`，而非 `StageConfig` 构造时——所以单独构造 `StageConfig` 不会立刻报错，要等它进入 `PipelineConfig` 才会被拦。这正是「派生与校验放在管线级」的体现（见 4.4）。

> 是否需要 GPU：构造 config 对象本身不碰 GPU，但 `import sglang_omni.config.schema` 的导入链可能加载 torch；实际耗时**待本地验证**。若想最轻量，可只读 schema.py 源码而不真跑 REPL。

#### 4.1.5 小练习与答案

**练习 1**：一个阶段 `next=["x","y"]` 同时设了 `route_fn`，这合法吗？为什么？
**答**：合法。`next` 是静态拓扑全集声明（用于校验与拓扑推导），`route_fn` 是请求感知的运行时窄化，二者并不冲突；规则是 `next` 与 `terminal` 二选一，而 `route_fn` 是 `next` 的可选覆盖。见 [config.md:91-94](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L91-L94)。

**练习 2**：为什么 `factory_args` 里写 `gpu_id` 会被拒绝？
**答**：因为 `gpu_id` 归放置规划（placement）所有，由 worker 在 import 工厂后注入；要设设备应该用 `StageConfig.gpu`。见 [config.md:76](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L76)。

### 4.2 fan-in：wait_for / wait_for_fn / merge_fn

#### 4.2.1 概念说明

fan-in（扇入）是 fan-out 的反操作：多个上游阶段的结果**汇聚**到一个下游阶段。例如 Qwen3-Omni 里，`image_encoder` 和 `audio_encoder` 的输出要和 `preprocessing` 的结果一起，汇到 `mm_aggregate` 才能拼成 thinker 的输入。

fan-in 三件套：

- `wait_for`：上游阶段的**静态全集**（所有可能的上游）。
- `wait_for_fn`：请求感知窄化——根据本次请求的内容，返回 `wait_for` 的一个非空子集，或 `None`（表示「现在还定不下来，先挂起等下一个上游负载」）。
- `merge_fn`：把收齐的多个上游负载**合并成一个**负载的函数。**设了 `wait_for` 就必须设 `merge_fn`**。

#### 4.2.2 核心流程

fan-in 的运行时流程（详见 u3-l1 的 `InputHandler` / `AggregatedInput`）：

1. Stage 收到某个上游的 `DataReadyMessage`；
2. 调 `wait_for_fn(request_id, from_stage, payload)` 算出「本次还需等待的活跃上游子集」；
3. 若子集还没收齐，挂起；若返回 `None`，保持 pending；
4. 收齐后，调 `merge_fn` 把多个上游负载合并；
5. 把合并结果作为一条任务推入 `scheduler.inbox`。

关键不变量：所有「真正可执行的工作」最终都汇成一条推入 `scheduler.inbox` 的任务——Stage 只是个 IO 外壳，自己不分支于 scheduler 类型。

#### 4.2.3 源码精读

Qwen3-Omni 的 `mm_aggregate` 阶段是教科书级的 fan-in：

[sglang_omni/models/qwen3_omni/config.py:89-122](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L89-L122) — 三件套同时出现：
- `wait_for=["preprocessing","image_encoder","audio_encoder"]`（全集）；
- `wait_for_fn=...resolve_mm_aggregate_wait_sources`（窄化）；
- `merge_fn="sglang_omni.models.qwen3_omni.merge.merge_for_thinker"`（**这就是声明 fan-in 合并函数的字段**）。

`wait_for_fn` 的真实实现——它演示了「返回 `None` 让请求继续挂起」的模式：

[sglang_omni/models/qwen3_omni/request_builders.py:123-132](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L123-L132) — 只有当 `from_stage == "preprocessing"` 时，它才根据预处理产物里「哪些编码器有真实输入」算出活跃子集（如 `["preprocessing","image_encoder"]`）；其它上游到达时返回 `None`，表示「等 preprocessing 先来定调子」。

#### 4.2.4 代码实践

**目标**：读懂 `wait_for_fn` 的「两段式」语义。

**操作步骤**：阅读 [request_builders.py:123-132](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L123-L132) 与 [request_builders.py:372-379](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L372-L379)（`_active_encoder_stages`）。

**需要观察的现象**：函数签名是 `(request_id, from_stage, payload) -> list[str] | None`；只有 preprocessing 到达时返回非 `None`。

**预期结果**：你应当能用自己的话解释——**为什么把「算子集」的时机绑在 preprocessing 到达那一刻**：因为只有 preprocessing 产物里的 `encoder_inputs` 才告诉管线「这次请求有没有图、有没有音」，在它之前无法确定子集。**待本地验证**：若你想看真实调用，可在 `resolve_mm_aggregate_wait_sources` 末尾加一行 `print` 观察一次语音请求的 `from_stage` 序列。

#### 4.2.5 小练习与答案

**练习 1**：设了 `wait_for` 却忘设 `merge_fn` 会怎样？
**答**：构造 `PipelineConfig` 时 `_validate_general` 抛 `Stage ... has wait_for but no merge_fn`。见 [schema.py:365-367](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L365-L367)。

**练习 2**：`wait_for_fn` 返回的子集必须满足什么约束？
**答**：必须非空、且是 `wait_for` 的子集；返回 `None` 表示暂时挂起、等下一个上游负载再定。见 [config.md:95-99](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L95-L99)。

### 4.3 流式边与负载投影：stream_to / stream_done_to_fn / project_payload

#### 4.3.1 概念说明

有两种「下游边」容易混淆，务必分清：

- **常规结果边（`next`）**：一个请求在该阶段**算完**后，把最终结果发往下游。一次请求走一次。
- **流式边（`stream_to`）**：阶段在**还没算完**时，就边算边把中间块（如 thinker 的 hidden state、codec code）流给下游。一条流式边在一次请求里会走很多次。

`stream_to` 同样遵循「静态全集」范式：写成所有可能的流式目标；`stream_done_to_fn` 在请求感知层面挑出「这次该给谁发结束信号」。运行时预备阶段会从 `stream_to` 派生出「流接收方」和「同 GPU 的流式快路径」（后者走 CUDA IPC 直传，见 u3-l3 / u6-l1）。

`project_payload`（负载投影）解决另一个问题：**同一段产物，发给不同下游时要裁剪成不同视图**。比如 `preprocessing` 的产物里既有图像输入又有音频输入，发给 `image_encoder` 时只投影出图像那部分，发给 `audio_encoder` 时只投影出音频那部分。

#### 4.3.2 核心流程

一个流式阶段（如 thinker）的边集合可以这样记：

```
thinker ──next──► decode            （常规结果：最终 token 序列）
        ──stream_to──► talker_ar     （流式：逐 token 的 hidden state）
        ──stream_to──► decode        （流式：逐 token 的 id，供 decode 提前出字）
```

`stream_done_to_fn` 决定「当 thinker 流完，结束信号发给谁」——语音请求发给 `[talker_ar, decode]`，纯文本请求只发给 `[decode]`。

#### 4.3.3 源码精读

thinker 阶段把流式边用到了极致：

[sglang_omni/models/qwen3_omni/config.py:125-152](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L125-L152) — `stream_to=["talker_ar","decode"]`（语音）或 `["decode"]`（文本）是全集；`stream_done_to_fn=...resolve_thinker_stream_done_targets` 是窄化；`project_payload={"decode": ...}` 给常规结果边配投影。

两个请求感知函数的实现都依赖同一个判据——`should_generate_audio_output`：

[sglang_omni/models/qwen3_omni/request_builders.py:97-109](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L97-L109) — `resolve_thinker_stream_done_targets` 与 `resolve_terminal_stages` 都是「按 `output_modalities` 里有没有 audio」二选一。注意 `resolve_terminal_stages` 返回的就是 u2-l4 里 Coordinator 读取的「动态期望终态」：语音请求要同时等 `decode`（文本）和 `code2wav`（音频）两个终态。

`project_payload` 的真实投影函数示例——把 talker→code2wav 的负载裁成一个「请求闩锁」，真正的 codec 张量是走流式边过去的：

[sglang_omni/models/qwen3_omni/request_builders.py:164-170](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L164-L170) — `project_talker_to_code2wav` 返回 `data={}` 的空负载，因为 `code2wav` 设了 `can_accept_stream_before_payload=True`，codec 张量已通过 `stream_to=["code2wav"]` 流过去了。

#### 4.3.4 代码实践

**目标**：区分「常规边」与「流式边」在一个阶段上可以并存。

**操作步骤**：对照 [config.py:165-197](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L165-L197)（`talker_ar` 阶段）。

**需要观察的现象**：`talker_ar` 同时有 `next="code2wav"`（常规边）和 `stream_to=["code2wav"]`（流式边），二者指向同一个下游但用途不同。

**预期结果**：你应能用一句话说清——**常规边负责「请求闩锁 + 最终收尾」，流式边负责「实时搬运 codec 张量」**；二者都指向 `code2wav`，但 `code2wav` 靠 `can_accept_stream_before_payload=True` 先吃流、再用常规负载收尾。

#### 4.3.5 小练习与答案

**练习 1**：设了 `stream_done_to_fn` 却没设 `stream_to` 会怎样？
**答**：非法。校验抛 `cannot set stream_done_to_fn without stream_to`，因为运行时预备要从 `stream_to` 派生流接收方。见 [schema.py:349-352](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L349-L352)。

**练习 2**：`project_payload` 的 key 和 value 各是什么？
**答**：key 是**目标 stage 名**，value 是**点号投影函数路径**；在写出下游负载前调用，把负载裁成该目标需要的视图。见 [config.md:88](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L88)。

### 4.4 CommConfig：只调参，不选传输

#### 4.4.1 概念说明

`CommConfig` 是**单阶段**的通信调参块。最容易踩的坑：**它不负责选择传输后端**。传输选择（local_object / cuda_ipc / shm / mooncake）是由 `CommRouter` 根据「两个阶段的局部性与放置」**派生**出来的（见 u6-l1）。`CommConfig` 只在「路由器已经选定某种传输」之后，调一调该传输的缓冲池和后端连接参数。

#### 4.4.2 核心流程

`CommConfig` 的字段都是「调参旋钮」，不改变拓扑、不改变传输选型：

| 字段 | 默认 | 调什么 |
| --- | --- | --- |
| `slot_size_mb` | 512 | 通信缓冲槽大小 |
| `credits` | 2 | 基于信用的流量控制额度 |
| `cuda_ipc_slot_size_kb` | 64 | CUDA IPC 槽粒度 |
| `cuda_ipc_pool_size_mb` | None | CUDA IPC 发送池总大小 |
| `mooncake_protocol` | "rdma" | Mooncake 后端协议 |
| `mooncake_hostname` | None | Mooncake 主机名 |
| `mooncake_device_name` | "" | Mooncake 设备名 |

#### 4.4.3 源码精读

`CommConfig` 的定义极简，且用 `extra="forbid"` 禁止写未知字段：

[sglang_omni/config/schema.py:11-27](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L11-L27) — 注意类文档串明确写道：*Transport selection is owned by CommRouter ... This config only tunes buffer pools and backend-specific connection options.* 这就是「只调参、不选传输」的契约。

文档里也重申了这条边界：

[docs/developer_reference/config.md:133-135](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L133-L135) — *It does not select a transport backend.*

#### 4.4.4 代码实践

**目标**：验证「`CommConfig` 不影响传输选型」。

**操作步骤**：在 [schema.py:11-27](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L11-L27) 与 `StageConfig.comm` 字段（[schema.py:186](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L186)）里查找，确认没有任何 `transport` / `backend` 之类的字段。

**需要观察的现象**：`CommConfig` 只有缓冲与 Mooncake 参数，没有「选哪种传输」的开关。

**预期结果**：你能下结论——**要在同进程/同节点/跨节点之间切传输，改的是 stage 的放置（`gpu` / `process`），不是 `comm`**；`comm` 只在选定传输后微调缓冲。

#### 4.4.5 小练习与答案

**练习**：想让两个阶段的张量走 Mooncake RDMA，应该改 `CommConfig.mooncake_protocol` 还是改放置？
**答**：传输类型由放置与局部性派生（跨节点才会选 mooncake）；`mooncake_protocol="rdma"` 只在「路由器已经决定用 mooncake」之后才生效。所以**先靠放置让两个阶段落在不同节点**，`mooncake_*` 才有意义。见 [config.md:109-110](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L109-L110) 与 u6-l1。

### 4.5 PipelineConfig 与派生字段

#### 4.5.1 概念说明

`PipelineConfig` 是顶层容器：装着 `model_path`、`stages` 列表，以及一组「派生结论」的入口。它也是各模型家族要继承的基类——子类用 `ClassVar` 声明 `architecture`（HF 架构名，供 registry 匹配）、用 `Field(default_factory=...)` 给出该家族的默认 stage 列表。

派生字段（derived）是本模块的核心思想：**结论由 stage 算出来，不要手填**。三个最关键：

- `resolved_entry_stage`：入口阶段。显式设了 `entry_stage` 就用它，否则取 `stages[0].name`。
- `terminal_stages`：所有 `terminal=True` 的阶段名列表。
- `gpu_placement`：所有设了 `gpu` 的阶段 → 其卡号（或卡号列表）。

还有一个请求感知的「动态终态」入口 `terminal_stages_fn`——静态 `terminal_stages` 是全集，`terminal_stages_fn` 按请求返回本次真正期望的终态子集。

#### 4.5.2 核心流程

配置的生命周期分两段（承接 u1-l5 与 [config.md:156-197](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L156-L197)）：

1. **构造期**：实例化 `PipelineConfig` → `model_post_init` 调 `_validate_general` + `_validate_fusion` → 自动写入 `config_cls = 类名`、`name = model_path`（若未设）。
2. **运行时预备期（runtime prep）**：校验拓扑 → 算入口/终态 → 分配 ZMQ 端点 → 合并 `factory_args` 与 `runtime_overrides` 与类型化运行时字段（**不 import 工厂**）→ 准备签名相关默认值 → 构建 relay 配置 → 接好流式目标与同 GPU 快路径。

派生字段可看作第 2 步的「输入」。数学上，若记阶段集合为 \(S\)，则：

\[
\text{terminal\_stages} = \{\, s.\text{name} \mid s \in S,\ s.\text{terminal} \,\}
\]

\[
\text{gpu\_placement}: s.\text{name} \mapsto s.\text{gpu} \quad \text{当且仅当 } s.\text{gpu} \neq \text{None}
\]

#### 4.5.3 源码精读

派生属性都是 `@property`，从 `self.stages` 实时算出：

[sglang_omni/config/schema.py:248-256](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L248-L256) — `resolved_entry_stage` 与 `terminal_stages` 的实现；注意它们没有任何「手动赋值」分支，只读 `entry_stage` 与各 stage 的 `terminal` 标志。

[sglang_omni/config/schema.py:316-322](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L316-L322) — `gpu_placement` 遍历 `self.stages`，只收 `gpu is not None` 的阶段。

模型家族如何挂上「动态终态」：Qwen3-Omni 的语音管线在类体里设了 `terminal_stages_fn`：

[sglang_omni/models/qwen3_omni/config.py:336](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L336) — 指向 `resolve_terminal_stages`，其实现见 [request_builders.py:106-109](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L106-L109)：语音请求返回 `[decode, code2wav]`，纯文本返回 `[decode]`。**这正是 u2-l4 里 Coordinator「合并多终态」所读的那个动态期望集合。**

校验逻辑（`_validate_general`）把所有不一致挡在启动前——例如「非 TP 阶段必须声明 process」：

[sglang_omni/config/schema.py:399-406](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L399-L406) — 这是「没有隐式默认 process」这条强约束的来源：旧配置不会自动迁移，要手动给每个非 TP 阶段补 `process="pipeline"`（单进程）或别的名字。

#### 4.5.4 代码实践

**目标**：观察派生字段从 stage 列表「算出来」的过程。

**操作步骤**：阅读 [schema.py:248-256](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L248-L256) 与 [schema.py:316-322](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L316-L322)，然后对照 [qwen3_omni/config.py:286-304](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L286-L304)（`Qwen3OmniPipelineConfig`，6 阶段文本管线）。

**需要观察的现象**：文本管线的 `stages` 由 `_text_stages()` 生成，最后一个阶段是 `decode`（`terminal=True`）。

**预期结果**：手算后应得 `resolved_entry_stage="preprocessing"`、`terminal_stages=["decode"]`、`gpu_placement={"image_encoder":0,"audio_encoder":0,"mm_aggregate":0,"thinker":0}`（decode 是 CPU 终态，不出现在 `gpu_placement` 里）。**待本地验证**：可在装好环境的容器里 `from sglang_omni.models.qwen3_omni.config import Qwen3OmniPipelineConfig; c = Qwen3OmniPipelineConfig(model_path="..."); print(c.terminal_stages, c.gpu_placement)` 复核（import 链较重，属正常）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `resolved_entry_stage` / `terminal_stages` / `gpu_placement` 设计成 `@property` 而不是普通字段？
**答**：因为它们是 stage 列表的「投影」——若做成可写字段，就会出现「声明了终态但 stage 漏标 terminal」的不一致；做成派生属性保证配置永远自洽。

**练习 2**：静态 `terminal_stages` 与 `terminal_stages_fn` 是什么关系？
**答**：前者是 `terminal=True` 的全集（用于校验、用于「单终态」快路径），后者按请求返回本次真正期望的子集（用于「多终态合并」，承接 u2-l4）。`terminal_stages_fn` 返回 `None` 时回退到静态终态。见 [config.md:124](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L124)。

## 5. 综合实践：手绘 Qwen3-Omni 语音管线拓扑

**任务**：阅读 [examples/configs/qwen3_omni_colocated_h100_bf16.yaml](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/examples/configs/qwen3_omni_colocated_h100_bf16.yaml) 与它继承的 `Qwen3OmniSpeechColocatedPipelineConfig`（[config.py:352-373](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L352-L373)），手绘出该管线的 stage 拓扑，并指出三类边与 fan-in 的 merge 字段。

**步骤**：

1. 该 yaml 只覆盖了 `stage_overrides`（各 stage 的 `runtime.resources.total_gpu_memory_fraction`），拓扑本身来自 `config_cls: Qwen3OmniSpeechColocatedPipelineConfig`。先回到 [config.py:223-257](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L223-L257)（`_speech_stages`）看清 8 个阶段。
2. 画出拓扑（参考下面这张图，**先自己画再看**）：

```
                       ┌──► image_encoder ──┐
preprocessing ──fanout─┼──► audio_encoder ──┼──► mm_aggregate ──► thinker ──next──► decode  (terminal)
                       └────────────────────┘        ▲              │
                          (project_payload)        fan-in         stream_to ──► talker_ar ──next──► code2wav (terminal)
                       wait_for/merge_fn  (merge_for_thinker)        │  stream_to └─► decode
```

3. 在图上标注三类边：
   - **fan-out**（普通多路 `next` + `project_payload`）：`preprocessing → {image_encoder, audio_encoder, mm_aggregate}`；
   - **fan-in**（`wait_for` + `wait_for_fn` + `merge_fn`）：`{preprocessing, image_encoder, audio_encoder} → mm_aggregate`；
   - **stream**（`stream_to` + `stream_done_to_fn`）：`thinker → {talker_ar, decode}`、`talker_ar → code2wav`。
4. **回答关键问题**：声明 fan-in 合并函数的字段是 **`StageConfig.merge_fn`**，它在 `mm_aggregate` 阶段取值为 `sglang_omni.models.qwen3_omni.merge.merge_for_thinker`（见 [config.py:102](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L102)），与它成对出现的还有 `wait_for`（[config.py:100](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L100)）和 `wait_for_fn`（[config.py:101](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L101)）。

**预期结果**：你能指着图说出——「`next` 是普通边、`stream_to` 是流式边、`wait_for+merge_fn` 是 fan-in」；并且明白 yaml 里的 `total_gpu_memory_fraction` 只是改了这些阶段在本机的显存预算，**完全没动拓扑**。这正好印证 u1-l5 的结论：拓扑由模型家族决定，配置只决定「该拓扑在本机怎么跑」。

> 待本地验证：若想看真实派生结果，可在容器里 `sgl-omni config view --model-path Qwen/Qwen3-Omni-30B-A3B-Instruct` 观察导出的 `config_cls` 与 `stages`，对照你画的图。

## 6. 本讲小结

- `StageConfig` 的字段分 6 组：身份构造、普通路由、GPU 并行、运行时意图、fan-in、流式与投影；**`next` 与 `terminal` 二选一**，`route_fn` 是 `next` 的请求感知覆盖。
- **静态超集 + 请求感知窄化**是配置的核心范式：`next`/`wait_for`/`stream_to` 写全集，`route_fn`/`wait_for_fn`/`stream_done_to_fn` 在运行时挑子集。
- fan-in 三件套 `wait_for` + `wait_for_fn` + `merge_fn` 缺一不可；**`merge_fn` 就是声明 fan-in 合并函数的字段**。
- `CommConfig` **只调参、不选传输**；切传输要改放置（`gpu`/`process`），传输选型由 `CommRouter` 派生（u6-l1）。
- `resolved_entry_stage` / `terminal_stages` / `gpu_placement` 是从 stage 列表**派生**的，不该手填；`terminal_stages_fn` 是「多终态合并」的请求感知入口（承接 u2-l4）。
- 配置层是**静态契约**：把拓扑、放置、终态在运行时启动前全部固化，并把不一致在 `_validate_general` / `_validate_fusion` 阶段拦下。

## 7. 下一步学习建议

- 下一讲进入 **u3 单元（Pipeline 与 Stage 编排机制）**：建议先读 **u3-l1（Stage 抽象与 IO 外壳）**，看本讲声明的 `next`/`wait_for`/`stream_to` 在运行时如何被 `InputHandler` / `AggregatedInput` 真正消费。
- 想理解「进程拓扑如何由 `process`/`tp_size`/`fused_stages` 求解」，直接读 **u3-l4（进程拓扑与多进程 Runner）**，对应 [config.md:156-197](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L156-L197) 与 `sglang_omni/config/topology.py`。
- 想深入「传输如何由放置派生」，读 **u6-l1（通信路由与传输选择）**。
- 若想再看一个真实家族如何用同一套字段，读 [sglang_omni/models/qwen3_tts/config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/config.py) 与 **u5-l3（TTS 模型接入流程）**。
