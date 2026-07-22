# 配置文件七段结构

## 1. 本讲目标

本讲承接 u2-l1，把「一个 YAML 就是一次 run」这句话落到代码层面。读完本讲，你应当能够：

- 说出 run config 的**七段结构**（`model` / `data` / `training` / `tracking` / `profiling` / `runtime` / `deployment`）以及两个顶层字段 `run_id`、`output_dir` 分别管什么。
- 分清 `model` / `data` / `training` 三段里**最常用的核心字段**，并知道哪些字段是必填、哪些有默认值。
- 在 [specforge/config/schema.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py) 里快速找到每个段对应的 Pydantic 配置类。
- 独立写出一份最小的 EAGLE3 离线训练 YAML，并讲清 `training.strategy` 是如何被映射到具体算法的。

## 2. 前置知识

本讲默认你已经掌握 u2-l1 中建立的两条正交轴：

- **数据轴（online / offline）**：由 `data` 段决定。填了 `data.hidden_states_path` 就是 offline（读取预算好的特征文件），否则是 online（训练时实时捕获目标模型特征）。
- **部署轴（local_colocated / disaggregated）**：由 `deployment.mode` 决定。前者在本地把捕获和训练放在一起，后者把 producer（捕获）和 consumer（训练）拆成不同进程。

还需要两个 u1-l5 引入的概念：

- **类型化配置（typed config）**：SpecForge 用 Pydantic 描述 YAML。解析时会做类型转换与校验，**任何未知字段直接报错**，不存在「写错了被静默忽略」。
- **组合根（composition root）**：配置只是「契约」，真正把算法、模型、训练器装配起来发生在 `application/composition.py`，本讲只到「配置如何被解析与校验」为止，装配细节留给 u3-l4。

> 小提示：本讲反复出现两个容易混的「mode」——一个是数据模式（online/offline，是个**推导属性**），一个是部署模式（`deployment.mode`，是个**显式字段**）。看到 `mode` 时请先问自己指的是哪一个。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [specforge/config/schema.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py) | 全部配置类的唯一事实来源：七个 `*Config` 类、顶层 `Config`、加载与覆盖函数 |
| [docs/basic_usage/training.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md) | 官方对 run config 七段结构、默认值、支持组合的说明 |
| [examples/configs/qwen3-8b-eagle3-disaggregated.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-disaggregated.yaml) | 在线 disaggregated 模式的黄金样例 YAML |
| [examples/configs/qwen3-8b-eagle3-offline.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-offline.yaml) | 离线 colocated 模式的黄金样例 YAML（本讲综合实践会参考它） |
| [specforge/algorithms/builtin.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py) | 内置算法注册表，`strategy` 字符串最终在这里被解析 |

## 4. 核心概念与源码讲解

### 4.1 顶层聚合 Config：七个段 + 两个顶层字段

#### 4.1.1 概念说明

SpecForge 不再用「一个脚本一套参数」的老做法。schema.py 的模块文档明确写道：它描述的是一个 **run（一次训练运行）**，而不是一个遗留 Python 脚本——模型装配、prompt 准备、拓扑、策略专属目标函数，全部藏在这一份经过校验的契约背后。

[specforge/config/schema.py:L9-L15](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L9-L15) 这段模块文档说明：「YAML/JSON 文件和点号 CLI 覆盖都走同一个 schema 重新校验」。

一次 run 的配置由 **七个段** 加 **两个顶层字段** 组成：

| 类别 | 字段 | 是否必填 | 默认值 |
| --- | --- | --- | --- |
| 段 1 | `model` | 必填 | — |
| 段 2 | `data` | 必填 | — |
| 段 3 | `training` | 可省 | 全默认 |
| 段 4 | `tracking` | 可省 | 全默认 |
| 段 5 | `profiling` | 可省 | 全默认 |
| 段 6 | `runtime` | 可省 | 全默认 |
| 段 7 | `deployment` | 可省 | 全默认 |
| 顶层 | `run_id` | 可省 | `specforge-run` |
| 顶层 | `output_dir` | 可省 | `./output` |

注意：**只有 `model` 和 `data` 是真正必填的段**，其余五个段都有 `default_factory`，省略时全部使用默认值。

#### 4.1.2 核心流程

一份 YAML 被解析为 `Config` 对象的过程：

```text
specforge train --config run.yaml
        │
        ▼
Config.from_file(path)            # 读文件，yaml/json 解析成 dict
        │
        ▼
migrate_legacy_config(raw)        # 把遗留旧字段翻译成新结构（仅迁移层）
        │
        ▼
Config.model_validate(...)        # Pydantic 逐段构造 + 校验器
        │   ├── _default_role_for_deployment (before)  # 按 deployment.mode 推导 role 默认值
        │   └── _validate_run_structure   (after)      # 跨字段拓扑校验（不解析算法）
        ▼
Config 对象（带 .mode 属性、validate_world_size 方法）
```

关键点：校验器 `_validate_run_structure` 的职责注释明确写着「校验拓扑与跨字段形状，**不解析算法**」。也就是说 `strategy` 在这一层只是个普通字符串，真正的算法解析发生在更后面的组合根（u3-l4）。

#### 4.1.3 源码精读

**七个段 + 两个顶层字段** 的声明在顶层 `Config` 类里：

[specforge/config/schema.py:L644-L653](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L644-L653) 声明了七段与两个顶层字段，其中 `model`、`data` 无默认值（必填），其余带 `default_factory`。

```python
class Config(StrictConfigModel):
    model: ModelConfig
    data: DataConfig
    training: TrainingConfig = Field(default_factory=TrainingConfig)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    profiling: ProfilingConfig = Field(default_factory=ProfilingConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    deployment: DeploymentConfig = Field(default_factory=DeploymentConfig)
    run_id: str = "specforge-run"
    output_dir: str = "./output"
```

**「未知字段直接报错」的根** 来自 `StrictConfigModel`：

[specforge/config/schema.py:L32-L33](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L32-L33) 通过 `ConfigDict(extra="forbid")` 让所有配置类拒绝任何未声明字段。这就是 u2-l1 说的「拼错或已退役的选项不会被静默忽略」的实现。

**数据模式是个推导属性**，不是显式字段：

[specforge/config/schema.py:L859-L861](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L859-L861) `mode` 属性根据 `data.hidden_states_path` 是否为空来推导 `offline` / `online`。

```python
@property
def mode(self) -> str:
    return "offline" if self.data.hidden_states_path else "online"
```

这解释了为什么你在 YAML 里**找不到** `mode: offline` 这种顶层字段——它只能由 `data` 段间接决定。

**加载入口**：

[specforge/config/schema.py:L880-L889](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L880-L889) `Config.from_file` 按扩展名选择 yaml/json 解析，再经过 `migrate_legacy_config` 后交给 `model_validate`。

[specforge/config/schema.py:L921-L925](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L921-L925) `load_config(path, overrides)` 是对外入口：先 `from_file`，如果有覆盖再调 `apply_overrides`（覆盖语法细节在 u2-l3 详讲）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「七段 + 两顶层字段」与「未知字段报错」。

**操作步骤**：

1. 在仓库根目录启动 Python（已按 u1-l2 装好 specforge）。
2. 用 Python 直接构造一个最小 `Config`，观察 `mode` 推导：
   ```python
   # 示例代码：仅用于理解，不是项目原有脚本
   from specforge.config.schema import Config
   cfg = Config.model_validate({
       "model": {"target_model_path": "Qwen/Qwen3-8B"},
       "data": {"hidden_states_path": "./cache/hidden_states/x"},
   })
   print(cfg.mode)            # 期望: offline
   print(cfg.training.strategy)  # 期望: eagle3（默认）
   print(cfg.run_id)          # 期望: specforge-run（默认）
   ```
3. 故意加一个不存在的字段，触发 `extra="forbid"`：
   ```python
   Config.model_validate({
       "model": {"target_model_path": "Qwen/Qwen3-8B", "oops": 1},
       "data": {"hidden_states_path": "./x"},
   })
   ```

**需要观察的现象**：第 3 步应抛出 Pydantic 的 `ValidationError`，提示 `model` 段出现了多余字段 `oops`。

**预期结果**：`mode` 自动变成 `offline`；非法字段被拒绝。**待本地验证**（具体报错文案以你本地 Pydantic 版本为准）。

#### 4.1.5 小练习与答案

**练习 1**：如果完全省略 `data` 段，`Config.model_validate({"model": {...}})` 会发生什么？

> **答案**：`data` 是必填段（无默认值），构造时会抛 `ValidationError`，提示缺少 `data`。这和 `training` 等段不同——后者可全省略。

**练习 2**：`cfg.mode` 在什么条件下返回 `"online"`？

> **答案**：当 `data.hidden_states_path` 为空字符串（默认）时返回 `"online"`；填了任何路径就返回 `"offline"`。

---

### 4.2 核心三段：model / data / training

#### 4.2.1 概念说明

这三段是一次 run 里信息量最大、改动最频繁的部分：

- **`model` 段**：告诉 SpecForge「老师是谁、草稿长什么样」。包含目标模型路径、草稿配置、词表映射、精度、各种 SGLang 捕获引擎调参。
- **`data` 段**：告诉 SpecForge「喂什么数据」。核心是「三个数据源三选一」约束，以及序列长度、chat 模板等。
- **`training` 段**：告诉 SpecForge「怎么训」。包含算法选择、超参、并行拓扑、检查点频率、损失相关旋钮。

#### 4.2.2 核心流程

三段的关系可以这样理解：

```text
model.draft_model_config  ──┐
model.target_model_path   ──┼──►  草稿模型如何构造（u4-l4 草稿注册表、u6-l1 装配）
model.vocab_mapping_path  ──┘

data.{train_data_path | prompts_path | hidden_states_path}  ──►  唯一数据源
                                                              │
                                                              └──►  同时决定 cfg.mode = online/offline

training.strategy    ──►  选哪个算法（eagle3/dflash/...）
training.{lr, batch, accum, tp, sp, ...}  ──►  优化器与并行拓扑
```

#### 4.2.3 源码精读

**ModelConfig 的关键字段**（[specforge/config/schema.py:L36-L73](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L36-L73)）：

| 字段 | 行号 | 含义 |
| --- | --- | --- |
| `target_model_path` | [L37](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L37) | 目标模型（老师）路径，**必填** |
| `draft_model_config` | [L41](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L41) | 草稿 JSON/目录/HF repo；EAGLE3/P-EAGLE/DFlash 可省略（从目标推导） |
| `draft_checkpoint_path` | [L44](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L44) | **仅权重热启动**，与 `resume_from` 互斥 |
| `target_backend` | [L53](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L53) | 固定为 `Literal["sglang"]`，online 必须是 sglang |
| `input_modality` | [L60](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L60) | 输入模态，内置算法仅支持 `text` |
| `vocab_mapping_path` | [L67](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L67) | t2d/d2t 词表映射文件，空串表示无 |
| `torch_dtype` | [L72](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L72) | 训练精度，默认 `bfloat16` |

> 注意 `target_backend` 现在只剩 `sglang` 一个取值——曾经的 in-process HF/custom 后端在 server-only 改造中被移除，命名它的配置会在加载时报错而不是被静默忽略（见 [L50-L53](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L50-L53) 的注释）。

**DataConfig 的「三选一」约束**（[specforge/config/schema.py:L125-L161](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L125-L161)）：

| 字段 | 行号 | 适用模式 |
| --- | --- | --- |
| `train_data_path` | [L127](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L127) | online，原始对话 JSON/JSONL |
| `prompts_path` | [L129](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L129) | online，已分词的 JSONL |
| `hidden_states_path` | [L131](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L131) | offline，预算特征目录 |
| `max_length` | [L136](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L136) | 序列长度上限，默认 2048 |
| `chat_template` | [L137](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L137) | 对话模板名，默认 `llama3` |

[specforge/config/schema.py:L148-L161](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L148-L161) `_exactly_one_source` 校验器强制三个数据源**必须且只能填一个**，否则报错。这是 u2-l1「`data` 段三选一」的实现。

**TrainingConfig 的常用字段**（[specforge/config/schema.py:L481-L547](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L481-L547)）：

| 字段 | 行号 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `strategy` | [L482](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L482) | `eagle3` | 算法名（见 4.4） |
| `num_epochs` | [L483](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L483) | `1` | 轮数 |
| `max_steps` | [L484](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L484) | `None` | 停止上限（optimizer step） |
| `batch_size` | [L486](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L486) | `1` | 批大小 |
| `accumulation_steps` | [L487](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L487) | `1` | 梯度累积步数 |
| `learning_rate` | [L489](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L489) | `1e-4` | 学习率 |
| `warmup_ratio` | [L490](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L490) | `0.015` | warmup 比例 |
| `max_grad_norm` | [L491](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L491) | `0.5` | 梯度裁剪 |
| `ttt_length` | [L495](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L495) | `7` | EAGLE3 训练时测试步数（见 u1-l4） |
| `attention_backend` | [L496-L498](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L496-L498) | `flex_attention` | 注意力后端 |
| `tp_size` / `sp_ulysses_size` / `sp_ring_size` | [L501-L503](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L501-L503) | `1` | 张量并行 / 序列并行 |
| `save_interval` | [L531](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L531) | `0` | 检查点频率 |
| `eval_interval` | [L533](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L533) | `0` | 评测频率（须配 eval 数据源） |
| `log_interval` | [L534](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L534) | `50` | 日志频率 |
| `max_checkpoints` | [L536](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L536) | `0` | 检查点轮转保留数 |
| `compact_teacher` | [L539](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L539) | `False` | 离线 EAGLE3 紧凑教师投影 |
| `resume_from` | [L542](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L542) | `None` | 完整续训（与 `draft_checkpoint_path` 互斥） |
| `role` | [L546](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L546) | 见下 | disaggregated 角色派生 |
| `seed` | [L547](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L547) | `42` | 随机种子 |

`role` 的默认值不是写死在字段里，而是在顶层 `Config` 的 `before` 校验器里按部署模式推导：

[specforge/config/schema.py:L655-L667](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L655-L667) `_default_role_for_deployment`：当 `deployment.mode=disaggregated` 时默认 `role=auto`，否则默认 `role=all`。这就是为什么 colocated 配置从不写 `role`，而 disaggregated 配置默认就是 `auto`。

#### 4.2.4 代码实践

**实践目标**：对照黄金样例，确认核心三段的字段都能在 schema 里找到出处。

**操作步骤**：

1. 打开 [examples/configs/qwen3-8b-eagle3-disaggregated.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-disaggregated.yaml)。
2. 对 `model:` 段里的每一个键（如 `target_model_path`、`draft_model_config`、`vocab_mapping_path`、`sglang_mem_fraction_static`），在 schema.py 的 `ModelConfig` 里找到对应字段声明与行号。
3. 对 `training:` 段做同样的事，注意 `ttt_length: 7`、`attention_backend: flex_attention` 等都能对上默认值。

**需要观察的现象**：样例里的每个键都在对应配置类里有声明；样例里**没有出现**的键（如 `optimizer_cpu_offload`、`compact_teacher`）则使用默认值。

**预期结果**：你能填出一张「YAML 键 → schema 字段 → 行号」对照表。这一步是纯阅读，**待本地验证**（用编辑器跳转即可）。

#### 4.2.5 小练习与答案

**练习 1**：`training.draft_checkpoint_path` 和 `training.resume_from` 都能恢复一个草稿，它们的区别是什么？

> **答案**：这里有个常见的坑——`draft_checkpoint_path` 属于 **`model` 段**（[L44](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L44)），只恢复**权重**，用于新 run 的热启动；`resume_from` 属于 **`training` 段**（[L542](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L542)），恢复**权重+优化器/调度器/步数/数据位置/RNG**，用于续训同一个 run。二者互斥（见 [L697-L704](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L697-L704)）。

**练习 2**：为什么在线训练的 YAML 里 `data` 段填的是 `train_data_path` 而不是 `hidden_states_path`？

> **答案**：`hidden_states_path` 触发 `cfg.mode="offline"`；在线训练需要 `mode="online"`，所以必须用 `train_data_path` 或 `prompts_path`（三选一约束 + mode 推导共同保证）。

---

### 4.3 辅助四段：deployment / tracking / profiling / runtime

#### 4.3.1 概念说明

剩下四段使用频率较低、很多情况下全部走默认即可，但理解它们的职责有助于调试与运维：

- **`deployment`**：进程拓扑——本地 colocated 还是 disaggregated，多少节点、每节点多少进程，disaggregated 的 Mooncake/shared_dir 传输后端。
- **`tracking`**：实验跟踪后端——wandb / tensorboard / swanlab / mlflow / none。
- **`profiling`**：每个 rank 的有界 PyTorch trace 窗口。
- **`runtime`**：在线 disaggregated 流式传输的水位线参数（producer 租约、在途高低水位、常驻内存上限）。

#### 4.3.2 核心流程

这四段在装配里的角色：

```text
deployment.mode ──► 决定 launch_plan（u3-l2）与 role 派生
deployment.trainer.{nnodes, nproc_per_node} ──► world_size = nnodes * nproc_per_node
tracking.report_to ──► 选哪个日志后端（u9-l3）
profiling.enabled ──► 是否在指定 optimizer step 窗口采 trace（u9-l3）
runtime.* ──► 在线 consumer 的流控（u7）
```

#### 4.3.3 源码精读

**DeploymentConfig**（[specforge/config/schema.py:L460-L478](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L460-L478)）：

- `mode`：`Literal["local_colocated", "disaggregated"]`，默认 `local_colocated`（[L463](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L463)）。
- `trainer`：`TrainerDeploymentConfig`，含 `nnodes`、`nproc_per_node`、`master_addr/port`（[L237-L259](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L237-L259)）。
- `disaggregated`：`Optional[DisaggregatedDeploymentConfig]`，仅在 `mode=disaggregated` 时必填（[L465](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L465)）。

[L467-L478](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L467-L478) `_validate_mode` 强制一对互斥关系：`mode=disaggregated` 必须配 `disaggregated` 段，反之亦然——避免「模式与拓扑配置不匹配」。

**TrackingConfig**（[specforge/config/schema.py:L164-L178](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L164-L178)）：

- `report_to`：`Literal["none", "wandb", "tensorboard", "swanlab", "mlflow"]`，默认 `none`（[L167](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L167)）。
- 其余是各后端专属的 project/name/key 字段，省略即不启用外部跟踪。

**ProfilingConfig**（[specforge/config/schema.py:L181-L187](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L181-L187)）：

```python
class ProfilingConfig(StrictConfigModel):
    enabled: bool = False
    start_step: int = Field(default=30, ge=0)
    num_steps: int = Field(default=4, gt=0)
    record_shapes: bool = False
```

注意窗口单位是「已完成的 optimizer step」，且 `producer` 角色不允许开 profiling（见 [L705-L709](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L705-L709)），因为 producer 只做捕获、没有训练循环。

**RuntimeConfig**（[specforge/config/schema.py:L190-L234](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L190-L234)）：在线 disaggregated 流控参数，如 `producer_lease`（[L193](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L193)）、`in_flight_high_watermark`（[L194](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L194)）。[L200-L234](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L200-L234) 的 `_validate_watermarks` 校验低水位不得超过高水位等约束。离线训练基本用不到这一段。

#### 4.3.4 代码实践

**实践目标**：阅读样例 YAML 的 `deployment`/`tracking` 段并定位字段出处。

**操作步骤**：

1. 回到 [qwen3-8b-eagle3-disaggregated.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-disaggregated.yaml)，观察 `deployment.mode: disaggregated` 与 `deployment.disaggregated:` 段同时出现，验证互斥校验的另一侧（成对出现）。
2. 对比离线样例 [qwen3-8b-eagle3-offline.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-offline.yaml)：它的 `deployment.mode: local_colocated` 且**没有** `disaggregated` 段。
3. 注意离线样例**完全没有** `runtime` 段——因为流控只对在线 disaggregated 有意义。

**需要观察的现象**：`mode` 与 `disaggregated` 段总是成对出现或成对缺失，正好对应 `_validate_mode` 的约束。

**预期结果**：你能解释为什么离线 colocated 配置最「干净」（只有 model/data/tracking/deployment 四段）。**待本地验证**（直接阅读两个 YAML 对比即可）。

#### 4.3.5 小练习与答案

**练习 1**：把 `deployment.mode` 设为 `disaggregated` 但不写 `disaggregated` 段，会怎样？

> **答案**：`_validate_mode`（[L467-L478](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L467-L478)）会抛错，提示 disaggregated 段必填。

**练习 2**：`profiling.enabled: true` 配在一个 `role: producer` 的进程上会怎样？

> **答案**：[L705-L709](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L705-L709) 明确拒绝：profiling 只对训练角色生效，producer 只做捕获。

---

### 4.4 strategy 字段如何选择算法

#### 4.4.1 概念说明

u2-l1 已经强调：所有草稿方法共用 `specforge train` 一个入口，靠 `training.strategy` 区分。本讲把这条「字符串 → 算法」的链路讲清楚。一个关键认知是：**在 schema 层，`strategy` 只是一个自由字符串，并不在这里被校验**；真正把它映射到一个可执行算法的工作发生在组合根（u3-l4 详讲）。本模块只看字符串这一端的定义，以及它最终能匹配到的注册表。

#### 4.4.2 核心流程

```text
training.strategy: str = "eagle3"        # schema 层：普通字符串，默认 eagle3
        │
        ▼  （配置加载不解析算法）
        │
        ▼  （组合根 application/composition.py 调用）
builtin_algorithm_registry()              # 构造不可变注册表（5 个内置算法）
        │
        ▼
registry.resolve(strategy)                # 按名字查 AlgorithmRegistration
        │
        ▼
registration.spec      -> 算法契约（FeatureContract 等）
registration.providers -> 可执行端口（模型/捕获/collator）
```

#### 4.4.3 源码精读

**字符串定义端**：[specforge/config/schema.py:L482](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L482) `strategy: str = "eagle3"`，普通 `str`，无 `Literal` 约束，所以**拼错的策略名在加载 YAML 时不会立刻报错**，要等到组合根解析时才失败。

**内置注册表**：[specforge/algorithms/builtin.py:L13-L16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py#L13-L16) `builtin_algorithm_registry()` 返回一个包含 5 个算法的不可变 `AlgorithmRegistry`：

```python
return AlgorithmRegistry((eagle3(), peagle(), dflash(), domino(), dspark()))
```

**每个算法的名字字符串** 来自各自 providers 的常量：

| 算法 | 常量定义 | 值 |
| --- | --- | --- |
| EAGLE3 | [eagle3/providers.py:L38](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L38) | `"eagle3"` |
| P-EAGLE | [peagle/providers.py:L30](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/peagle/providers.py#L30) | `"peagle"` |
| DFlash | [dflash/providers.py:L38](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L38) | `"dflash"` |
| Domino | [domino/providers.py:L34](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/domino/providers.py#L34) | `"domino"` |
| DSpark | [dspark/providers.py:L37](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dspark/providers.py#L37) | `"dspark"` |

`strategy` 必须等于其中之一（小写），否则后续 `resolve` 找不到。

**名字如何进入 spec**：[specforge/algorithms/eagle3/providers.py:L125-L126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L125-L126) 构造 `AlgorithmSpec(name=ALGORITHM_NAME, ...)`，把常量写进纯数据契约。EAGLE3 的契约还声明了 OFFLINE 与 STREAMING 两个 `FeatureContract`（[L133-L159](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L133-L159)），说明它既能离线也能在线——这是「方法 × 模式」支持矩阵的来源（详见 u4-l1）。

**注册表的按名查找**：[specforge/algorithms/registry.py:L78-L84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L78-L84) `AlgorithmRegistry.resolve(name)` 遍历注册项，按 `name` 匹配，找不到就抛 `KeyError` 并列出所有已注册算法名。这正是拼错的 `strategy` 最终被拦截的地方。

> 小结一句话：`strategy` 在 schema 里是「待解析的字符串」，在注册表里被「按名查表」变成 `AlgorithmRegistration`（含 `spec` 契约半 + `providers` 可执行半），后者再驱动装配。算法契约与注册表的完整细节见 u4-l1、u4-l2。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：对照 schema.py，为一次 EAGLE3 **离线**训练写一份最小 YAML（含 model/data/training/deployment 四段必需字段），并解释 `strategy` 如何选中算法。

**操作步骤**：

1. 新建文件 `my-eagle3-offline.yaml`（示例文件，非项目原有）：

   ```yaml
   # 示例代码：最小 EAGLE3 离线 colocated 配置
   model:
     target_model_path: Qwen/Qwen3-8B          # 必填：目标模型（老师）
     draft_model_config: configs/qwen3-8b-eagle3.json  # 草稿配置（EAGLE3 也可省略由目标推导）
     target_backend: sglang                     # 默认即 sglang，显式写出更清晰

   data:
     hidden_states_path: ./cache/hidden_states/qwen3-8b-sharegpt  # offline 数据源（三选一）
     max_length: 4096
     chat_template: qwen

   training:
     strategy: eagle3        # 选中 EAGLE3 算法
     num_epochs: 10
     batch_size: 1
     learning_rate: 1.0e-4
     save_interval: 1000

   run_id: my-eagle3-offline
   output_dir: ./outputs/my-eagle3-offline

   deployment:
     mode: local_colocated    # 离线本地 colocated
     trainer:
       nnodes: 1
       nproc_per_node: 1
   ```

2. 不启动训练，只用 `--plan` 预览解析后的计划（u2-l3 详讲覆盖语法，这里只验证 YAML 能被 schema 接受）：

   ```bash
   specforge train --config my-eagle3-offline.yaml --plan
   ```

3. 故意把 `strategy` 改成拼错的 `eagle333`，再次 `--plan`，观察报错出现的阶段。

**需要观察的现象**：

- 正确的 YAML 能通过 schema 校验，`--plan` 打印出一个 colocated 的进程计划。
- 拼错的 `strategy` **不一定在 `--plan` 阶段报错**（因为 schema 层不解析算法）；若计划阶段未拦截，它会在更后面的组合根 `resolve_run` 阶段抛 `KeyError: unknown algorithm 'eagle333'`。

**预期结果**：最小 YAML 通过校验；`strategy: eagle3` 与注册表里的 `ALGORITHM_NAME = "eagle3"` 字符串精确匹配，从而被 `registry.resolve` 查到。**待本地验证**（是否在 plan 阶段就解析算法，取决于 `--plan` 是否触达组合根，以本地行为为准）。

**如何解释 strategy 选择算法**（实践要求回答的问题）：

1. YAML 里 `training.strategy: eagle3` 被解析为 `TrainingConfig.strategy` 这个普通字符串（[schema.py:L482](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L482)）。
2. 组合根调用 `builtin_algorithm_registry()` 得到含 5 个算法的不可变注册表（[builtin.py:L16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py#L16)）。
3. 用 `registry.resolve("eagle3")` 按名查表（[registry.py:L78-L84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L78-L84)），匹配到 EAGLE3 的 `AlgorithmRegistration`（其 `spec.name` 来自 [eagle3/providers.py:L38](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L38) 的 `ALGORITHM_NAME`）。
4. 该注册项的 `spec`（契约）+ `providers`（可执行端口）随后驱动模型装配、特征捕获、损失计算（见 u4-l2、u6-l1、u6-l2）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `strategy` 拼成 `EAGLE3`（大写），会发生什么？

> **答案**：注册表按精确字符串匹配（`"eagle3"` 小写）。大写 `EAGLE3` 不匹配任何注册项，`resolve` 会抛 `KeyError` 并列出合法算法名。算法名还受 [contracts.py:L15](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L15) 的正则 `^[a-z][a-z0-9_-]*$` 约束，必须小写开头。

**练习 2**：为什么 schema 不直接用 `Literal["eagle3","peagle",...]` 约束 `strategy`？

> **答案**：SpecForge 的算法是**可扩展**的——用户能通过注册表新增算法（见 u10-l2）。如果把名字硬编码进 `Literal`，每新增一个算法就要改 schema，违背了「算法与配置解耦」的设计。所以 schema 只存字符串，解析延迟到组合根。

## 5. 综合实践

把本讲的知识串起来，完成一次「从空白到可校验」的配置编写与诊断。

**任务**：为 EAGLE3 离线 colocated 训练写一份最小 YAML，并验证它的每一处都能对回 schema，最后解释算法选中链路。

**步骤**：

1. 参考 [qwen3-8b-eagle3-offline.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-offline.yaml)，删掉所有可省略字段，只保留 model/data/training/deployment 四段的**必需**字段（见 4.4.4 的示例）。
2. 为你保留的每个字段，在 schema.py 里找到它的配置类与行号，填出一张对照表：

   | YAML 字段 | 配置类 | schema.py 行号 | 必填？ |
   | --- | --- | --- | --- |
   | `model.target_model_path` | `ModelConfig` | L37 | 是 |
   | `data.hidden_states_path` | `DataConfig` | L131 | 三选一 |
   | `training.strategy` | `TrainingConfig` | L482 | 否（默认 eagle3） |
   | `deployment.mode` | `DeploymentConfig` | L463 | 否（默认 local_colocated） |
   | …… | | | |
3. 回答三个诊断题：
   - 这份配置的 `cfg.mode` 是什么？为什么？（提示：[L859-L861](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L859-L861)）
   - 为什么这份配置不需要写 `runtime` 段？（提示：runtime 只服务在线 disaggregated 流控）
   - `strategy: eagle3` 经哪几步变成可执行算法？（提示：4.4.4 末尾的四步）
4. 运行 `specforge train --config my-eagle3-offline.yaml --plan`（不占 GPU），确认配置能被接受。

**验收标准**：YAML 通过 schema 校验；对照表每行行号准确；三道诊断题回答正确。实际启动训练需要真实的特征文件与权重，本任务**只要求通过校验/计划阶段**，完整训练留到具备数据后再做。

## 6. 本讲小结

- run config = **七段**（model/data/training/tracking/profiling/runtime/deployment）+ **两个顶层字段**（run_id/output_dir）；其中只有 `model`、`data` 必填，其余五段全省略即用默认值。
- 所有配置类继承 `StrictConfigModel`（`extra="forbid"`），**未知字段直接报错**；这是「拼错不会被静默忽略」的根本原因。
- 数据模式 `online`/`offline` 是 `Config.mode` **推导属性**，由 `data.hidden_states_path` 是否为空决定，不要和显式字段 `deployment.mode` 混淆。
- `data` 段三个数据源（`train_data_path`/`prompts_path`/`hidden_states_path`）**必须且只能填一个**，由 `_exactly_one_source` 强制。
- `training.strategy` 在 schema 层只是普通字符串，最终经 `builtin_algorithm_registry()` → `registry.resolve(name)` 按名查表，匹配到含 `spec`+`providers` 的 `AlgorithmRegistration`；内置五个算法名：`eagle3`/`peagle`/`dflash`/`domino`/`dspark`。
- `role` 默认值按 `deployment.mode` 推导：disaggregated→`auto`，否则→`all`；`draft_checkpoint_path`（仅权重）与 `resume_from`（完整续训）互斥。

## 7. 下一步学习建议

- 想掌握命令行覆盖与计划预览：进入 **u2-l3 命令行覆盖与 plan 预览**，学 `section.field=value` 语法与 `--plan`。
- 想理解配置之后是如何被装配成可执行 run 的：进入 **u3 单元（入口与启动链路）**，尤其是 **u3-l4 应用组合根 composition**，那里才是 `strategy` 真正被 `resolve_run` 解析的地方。
- 想深入算法契约本身：进入 **u4-l1 算法契约 contracts** 与 **u4-l2 算法注册表**，看清 `AlgorithmSpec`/`FeatureContract` 与 `AlgorithmRegistration` 的双半结构。
- 想看真实样例的更多组合：浏览 [examples/configs/](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs) 下的几十份 YAML，对照本讲的字段表加深印象。
