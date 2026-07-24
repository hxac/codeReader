# 配置查看、导出与 YAML 结构

## 1. 本讲目标

上一讲（u1-l4）你已经能用 `sgl-omni serve` 启动一个服务并发送第一条请求。本讲把镜头拉回到「服务启动之前」的一步：**配置（config）是怎么来的、长什么样、又能怎么改**。

读完本讲，你应当能够：

- 用 `sgl-omni config view` 把一个模型的默认管线配置打印出来看；
- 用 `sgl-omni config export` 把默认配置导出成一份可编辑的 YAML 文件；
- 读懂仓库里 `examples/configs/*.yaml` 这种「紧凑覆盖文件」的两段式结构（`config_cls` + `stage_overrides`）；
- 说清楚：为什么配置是「模型定义」和「模型无关运行时」之间的一份契约。

核心一句话：**模型家族决定管线的拓扑结构，配置文件决定这副拓扑在你这台机器上怎么跑。**

## 2. 前置知识

用最朴素的方式理解几个概念：

- **配置（config）**：一份「说明书」。它告诉运行时「有哪些阶段（stage）、每个阶段在哪块 GPU 上、每个阶段占多少显存」。运行时照着说明书搭管线，而不是把细节写死在代码里。
- **YAML**：一种用缩进表示层级的文本格式，人类好读好改。本讲的配置文件都是 YAML。
- **阶段（stage）**：一次生成被拆成的接力环节，比如 Qwen3-Omni 里的 `image_encoder`、`thinker`、`talker_ar`、`code2wav`（见 u1-l1 的七大阶段类别）。
- **运行时资源（runtime resources）**：每个阶段在运行时需要的「预算」，例如 `total_gpu_memory_fraction`（占整块 GPU 显存的比例）。这是本讲实践中你会动手改的字段。
- **契约（contract）**：模型家族提供的 `PipelineConfig` 描述了「这副管线有哪些阶段、怎么连」；运行时（Scheduler / ModelRunner / 传输层）只认这份结构化的配置，**不关心**具体是哪个模型。配置就是两者之间的接口。

你只需要记住：**改配置 = 改说明书，不动模型代码，也不动运行时代码。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sglang_omni/cli/config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/config.py) | `sgl-omni config` 子命令的入口，实现 `view` 与 `export` 两个命令 |
| [sglang_omni/config/manager.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py) | `ConfigManager`：管理「模型默认配置 / 配置文件 / 命令行参数」三种来源并合并；含 `resolve_config_cls_for_model_path` 与 `_apply_stage_overrides` |
| [sglang_omni/config/schema.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py) | `PipelineConfig` / `StageConfig` / `StageRuntimeConfig` / `StageResourceConfig` 的字段定义，是配置契约的「语法」 |
| [sglang_omni/models/registry.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py) | 模型注册表：把 HF architecture 映射到对应的 `PipelineConfig` 子类 |
| [examples/configs/qwen3_omni_colocated_h100_bf16.yaml](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/examples/configs/qwen3_omni_colocated_h100_bf16.yaml) | 一个真实的「紧凑覆盖文件」样例，演示 `config_cls` + `stage_overrides` 两段式写法 |
| [sglang_omni/cli/serve.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py) | `sgl-omni serve` 的入口，展示了三种配置来源在真实启动流程里如何被选用与合并 |

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块，按「配置从哪来 → 谁来管 → 怎么导出 → 怎么局部改」的顺序展开。

### 4.1 配置类解析：从 model_path 到 PipelineConfig

#### 4.1.1 概念说明

SGLang-Omni 支持很多模型家族（Qwen3-Omni、各类 TTS、ASR 等），每个家族都有自己的管线结构。**框架不要求你记住「哪个模型用哪份配置」**，而是给你一个 `model_path`（Hugging Face 模型 ID 或本地目录），框架自己去查出该用哪个配置类。

这套机制由函数 `resolve_config_cls_for_model_path` 实现。它的核心思路是：

1. 读取模型的 Hugging Face 配置（`config.json` 里的元数据，**不是模型权重**）；
2. 从中提取 `architecture`（架构名）；
3. 用架构名去模型注册表里查出对应的 `PipelineConfig` 子类。

这就是「配置是契约」的第一层含义：**模型的 architecture 决定它属于哪个家族，家族决定配置类，配置类决定管线拓扑。**

#### 4.1.2 核心流程

```text
model_path
   │  AutoConfig.from_pretrained(model_path)   ← 读 config.json 元数据（不需权重、不需 GPU）
   ▼
hf_config
   │  architecture_from_hf_config(...)          ← 取出 architecture 字符串
   ▼
architecture（如 "Qwen3OmniForCausalLM"）
   │  PIPELINE_CONFIG_REGISTRY.get_config(arch) ← 查注册表
   ▼
PipelineConfig 子类（如 Qwen3OmniSpeechColocatedPipelineConfig）
```

注册表本身是在 import 时自动扫描 `sglang_omni/models/<模型>/config.py` 里的 `EntryClass` 构建的（见 u5-l1 模型注册表），所以你「安装了哪些模型包，注册表里就有哪些 architecture」。

#### 4.1.3 源码精读

`resolve_config_cls_for_model_path` 是配置链路的起点，[sglang_omni/config/manager.py:16-31](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L16-L31)：

```python
def resolve_config_cls_for_model_path(model_path: str):
    hf_config = None
    try:
        hf_config = AutoConfig.from_pretrained(model_path)
    except (OSError, ValueError, KeyError):
        hf_config = None

    arch = architecture_from_hf_config(hf_config) if hf_config is not None else None
    if arch is None:
        arch = try_resolve_arch_from_raw_config(model_path)   # 兜底 1：手读 raw config
    if arch is None:
        arch = try_resolve_arch_from_mistral_config(model_path)  # 兜底 2：Mistral 特例
    if arch is None:
        raise ValueError(f"Could not resolve model architecture for {model_path!r}")
    return PIPELINE_CONFIG_REGISTRY.get_config(arch)
```

要点：

- `AutoConfig.from_pretrained` 只读 `config.json`，**不加载权重**；所以这一步不需要 GPU，也不需要下载几十 GB 的权重，只要模型的元数据可解析（本地有该目录，或可从 HF 下载 `config.json`）。
- 它准备了三条解析路径（标准 + 两个兜底），全部失败才抛 `ValueError`。
- 最后 `PIPELINE_CONFIG_REGISTRY.get_config(arch)` 在注册表里按 architecture 查类，查不到也抛错，[sglang_omni/models/registry.py:114-119](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py#L114-L119)。

#### 4.1.4 代码实践

**实践目标**：验证一个模型会被解析成哪个配置类，且这一步不需要 GPU。

**操作步骤**：

1. 确认 CLI 已安装（u1-l2）：`sgl-omni --help` 能看到 `config` 子命令。
2. 选一个你已经能拿到 `config.json` 的模型路径（例如本地已下载的 `Qwen/Qwen3-Omni-30B-A3B-Instruct`，或任意本地 checkpoint 目录）。
3. 运行：
   ```bash
   sgl-omni config view --model-path <你的模型路径> | head -n 20
   ```

**需要观察的现象**：输出的第一行附近会出现 `config_cls: <某个类名>`，例如 `Qwen3OmniSpeechColocatedPipelineConfig`。这就是解析结果。整个过程在没有 GPU 的机器上也能跑（只要 `config.json` 可解析）。

**预期结果**：能看到 `config_cls`、`model_path`、`stages:` 等字段被打印。如果报 `Could not resolve model architecture`，说明该模型的 architecture 没有被任何已安装的模型包注册，或 `config.json` 不可达。

> 注：本步骤的具体输出取决于你本地的模型路径，标记为「待本地验证」确切类名。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `resolve_config_cls_for_model_path` 只用 `AutoConfig.from_pretrained` 而不是去加载模型权重？

**参考答案**：它只需要知道模型的「架构名」来查注册表，而架构名写在 `config.json` 元数据里。读 `config.json` 远比加载几十 GB 权重便宜，也不需要 GPU——所以配置查看/导出是「元数据级」操作。

**练习 2**：如果传入一个完全没见过的模型 `model_path`，调用链会在哪一行失败？

**参考答案**：三个解析分支都返回 `None` 后，在 [manager.py:30](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L30) 抛 `Could not resolve model architecture`；如果架构名解析到了但注册表里没有，则在 [registry.py:115-118](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py#L115-L118) 抛 `not found in the pipeline config registry`。

---

### 4.2 ConfigManager：配置的三种来源与合并

#### 4.2.1 概念说明

`ConfigManager` 是配置体系的「中央调度」。它的 docstring 直白地说出了设计动机（[manager.py:36-40](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L36-L40)）：**omni 模型架构多样，不可能给它们定一套统一的命令行参数**，于是借鉴 TorchTitan 的做法，让用户可以「动态配置运行时设置」。

它管理三种配置来源，并按优先级把它们合并成一份最终的 `PipelineConfig`：

| 来源 | 工厂方法 | 何时用 |
| --- | --- | --- |
| 模型默认配置 | `from_model_path(model_path)` | 只有 `--model-path`，没给配置文件 |
| 配置文件 | `from_file(file_path)` | 用了 `--config <yaml>` |
| 命令行点式覆盖 | `merge_config(parse_extra_args(...))` | `sgl-omni serve ... --key=value` 里那些「未知参数」 |

合并的优先级是：**命令行覆盖 > 配置文件 > 模型默认**。`config view`/`export` 只产出「模型默认」这一层；要叠加覆盖，得在 `serve` 时通过 `--config` 或命令行参数完成。

#### 4.2.2 核心流程

`serve` 在真实启动里如何挑选来源（[serve.py:1216-1231](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1216-L1231)）：

```text
有 --config ?
   ├─ 是 → ConfigManager.from_file(config)
   ├─ 否，但 --text-only → ConfigManager.from_model_path(model_path, variant="text")
   └─ 否 → ConfigManager.from_model_path(model_path)

config_manager.parse_extra_args(ctx.args)   ← 把 --key=value 解析成 {key: value}
config_manager.merge_config(extra_args)      ← 点式合并 + 类型转换 + Pydantic 校验
→ merged_config（最终生效的 PipelineConfig）
```

`merge_config` 的关键能力是**点式路径（dotted path）赋值**：你可以写 `--stages.thinker.runtime.resources.total_gpu_memory_fraction=0.7`，它会沿点号一层层下钻（`_resolve_child`），甚至支持用阶段名当列表下标（`_resolve_list_index`，[manager.py:229-249](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L229-L249)）。最后用 `config_cls(**cfg_copy)` **重新校验**整个配置，所以非法值会在启动时就报错，而不是跑到运行时才崩。

#### 4.2.3 源码精读

`from_model_path` 调用上一节的解析函数拿到配置类、实例化、包成 `ConfigManager`，[manager.py:106-124](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L106-L124)：

```python
@staticmethod
def from_model_path(model_path, variant=None):
    config_cls = resolve_config_cls_for_model_path(model_path)
    if variant:
        module = importlib.import_module(config_cls.__module__)
        variants = getattr(module, "Variants", None)
        if variants and variant in variants:
            config_cls = variants[variant]          # 例如 text-only 变体
        else:
            raise ValueError(...)
    config = config_cls(model_path=model_path)
    return ConfigManager(config)
```

`from_file` 的逻辑是本讲的另一条主线——它既要支持「完整导出文件」，又要支持「紧凑覆盖文件」，[manager.py:126-144](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L126-L144)：

```python
@staticmethod
def from_file(file_path):
    with open(file_path) as f:
        data = yaml.safe_load(f)
    ...
    has_stage_overrides = "stage_overrides" in data
    stage_overrides = data.pop("stage_overrides", {})     # 把覆盖块单独拎出来
    config_cls_str = data["config_cls"]
    config_cls = PIPELINE_CONFIG_REGISTRY.get_config_cls_by_name(config_cls_str)
    config = config_cls(**data)                            # 用剩余字段重建完整配置
    if has_stage_overrides:
        config = _apply_stage_overrides(config, stage_overrides)  # 再叠加覆盖
    return ConfigManager(config)
```

注意它用的是 `get_config_cls_by_name`（按**类名**查，[registry.py:121-132](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py#L121-L132)），而不是按 architecture 查——因为配置文件里写的是类名字符串（如 `Qwen3OmniSpeechColocatedPipelineConfig`），不是 architecture 名。

#### 4.2.4 代码实践

**实践目标**：在真实启动代码里定位「三种来源」的抉择点。

**操作步骤**：

1. 打开 [serve.py:1216-1226](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1216-L1226)。
2. 找到 `if config:` / `elif text_only:` / `else:` 三个分支。
3. 继续往下读到 [serve.py:1230-1231](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1230-L1231) 的 `parse_extra_args` + `merge_config`。

**需要观察的现象**：命令行里 `ctx.args`（那些 Typer 不认识的 `--key=value`）会被 `parse_extra_args` 收集成字典，再由 `merge_config` 点式合并进配置——这正是 u1-l4 提到的「未知 `--key=value` 也会被当成配置覆盖」的实现位置。

**预期结果**：你能用一句话说出三者优先级：`--config` 文件先定基线，`--text-only` 选择变体，命令行点式参数最后覆盖。

#### 4.2.5 小练习与答案

**练习 1**：`from_file` 为什么用 `get_config_cls_by_name` 而不是 `get_config`？

**参考答案**：`get_config` 按 architecture 名查；而配置文件里 `config_cls` 字段存的是**类名字符串**（由 [schema.py:244](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L244) 的 `model_post_init` 自动写入），两者不是同一个键，所以要按类名查。

**练习 2**：`merge_config` 最后一步 `config_cls(**cfg_copy)` 有什么作用？

**参考答案**：它把「字典形式」的配置重新喂回 Pydantic 模型做**完整校验**——任何越界值（如 `total_gpu_memory_fraction` 不在 `(0, 1]`）都会在这里抛错，避免非法配置流入运行时。

---

### 4.3 YAML 导出：config view 与 config export

#### 4.3.1 概念说明

`sgl-omni config` 提供两个只读命令（[cli/__init__.py:9](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/__init__.py#L9) 把它注册成 `config` 子命令）：

- `config view`：把默认配置打印到**终端**，适合快速看一眼。
- `config export`：把默认配置写到一个 **YAML 文件**，适合拿去改、拿去用 `--config` 加载。

两者都只接受 `--model-path` 一个参数，背后的逻辑完全一致：解析配置类 → 实例化 → `model_dump(mode="json")` → `yaml.dump`。差别只在「输出到 stdout 还是文件」。

> 注意：这两个命令**不接受 `--config`**。它们导出的永远是「模型默认配置」。想要「导出 + 已合并覆盖」的效果，应该用 `sgl-omni serve --colocate` 或 `--log-level debug`，serve 会打印 `Merged Configuration`（见 [serve.py:83-99](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L83-L99)）。

#### 4.3.2 核心流程

```text
config_cls = resolve_config_cls_for_model_path(model_path)
config     = config_cls(model_path=model_path)        # 实例化默认配置
config_json = config.model_dump(mode="json")          # Pydantic → 纯 dict（JSON 兼容）
yaml.dump(config_json, sort_keys=False, ...)          # dict → YAML（保留字段顺序）
   ├─ view   : print(...)            到终端
   └─ export : open(output_path).write(...)  到文件
```

`model_dump(mode="json")` 会把所有嵌套模型（`StageConfig`、`StageRuntimeConfig`、`StageResourceConfig`…）展平成纯字典，并保留定义时的字段顺序（配合 `sort_keys=False`），所以导出的 YAML 读起来和 [schema.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py) 的字段顺序一致。

#### 4.3.3 源码精读

`view` 命令，[sglang_omni/cli/config.py:18-39](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/config.py#L18-L39)：

```python
@config_app.command()
def view(model_path: Annotated[str, typer.Option(...)]):
    """View the model's pipeline configuration."""
    config_cls = resolve_config_cls_for_model_path(model_path)
    config = config_cls(model_path=model_path)
    config_json = config.model_dump(mode="json")
    print(yaml.dump(config_json, sort_keys=False, default_flow_style=False,
                    indent=2, allow_unicode=True))
```

`export` 命令，[sglang_omni/cli/config.py:42-73](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/config.py#L42-L73)，逻辑几乎相同，只是把 `yaml.dump` 写进文件，并且 `--output-path` 可省（默认 `./config_<name>.yaml`，`name` 取自配置，[schema.py:245-246](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L245-L246) 默认回退到 `model_path`）：

```python
if output_path is None:
    output_path = f"./config_{config.name}.yaml"
with open(output_path, "w") as f:
    yaml.dump(config.model_dump(mode="json"), f, ...)
```

导出文件里会出现一个关键字段 `config_cls`：它在实例化时由 `model_post_init` 自动写上类名，[schema.py:241-244](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L241-L244)。正是这个字段让导出文件能被 `from_file` 重新读回（见 4.2.3）。

#### 4.3.4 代码实践

**实践目标**：导出一份完整的默认配置，看清每个 stage 的结构。

**操作步骤**：

1. 运行导出（`<模型路径>` 替换为你本地可解析的模型）：
   ```bash
   sgl-omni config export --model-path <模型路径>
   ```
2. 打开生成的 `./config_<name>.yaml`，定位到某个 stage（例如 `thinker`），展开它的 `runtime` 字段。

**需要观察的现象**：你会看到类似下面这样的完整结构（字段顺序与 schema 一致，**示例片段**）：

```yaml
# 示例片段（实际取值以本地导出为准）
config_cls: Qwen3OmniSpeechColocatedPipelineConfig
name: ...
model_path: ...
stages:
  - name: thinker
    factory: sglang_omni.models.qwen3_omni.stages.create_sglang_thinker_executor_from_config
    next:
      - decode
      - talker_ar
    stream_to:
      - talker_ar
    runtime:
      resources:
        total_gpu_memory_fraction: null   # 未显式设置时为 null
      max_seq_len: ...
      sglang_server_args:
        mem_fraction_static: null
    ...
```

**预期结果**：你能找到一个 stage 的 `runtime.resources.total_gpu_memory_fraction` 字段。没被模型默认显式设置时，它是 `null`（因为该字段默认 `None`，[schema.py:55-62](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L55-L62)）。这正是下一节你要改的字段。

#### 4.3.5 小练习与答案

**练习 1**：`view` 和 `export` 的代码几乎一样，为什么仓库要保留两个命令？

**参考答案**：`view` 输出到 stdout，适合「看一眼、管道处理」；`export` 落盘成文件，适合「拿去编辑后再用 `--config` 加载」。两者面向不同工作流，但核心序列化逻辑共用 `model_dump` + `yaml.dump`。

**练习 2**：导出的 YAML 里 `config_cls` 字段是哪来的？删掉它会怎样？

**参考答案**：由 `PipelineConfig.model_post_init` 在实例化后自动写入类名（[schema.py:244](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L244)）。如果删掉，`from_file` 会在 [manager.py:139](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L139) 取 `data["config_cls"]` 时抛 `KeyError`，文件无法被加载。

---

### 4.4 stage_overrides：紧凑覆盖文件

#### 4.4.1 概念说明

到这里会出现两种「合法 YAML」的形态，初学者很容易混淆，**这是本讲最重要的一处分清**：

| 形态 | 内容 | 体积 | 谁产生的 |
| --- | --- | --- | --- |
| **完整导出文件** | `config_cls` + 完整 `stages` 列表（每个 stage 全字段） | 大，几十到上百行 | `config export` 产出 |
| **紧凑覆盖文件** | `config_cls` + `model_path` + `stage_overrides`（只写想改的字段） | 小，十几行 | 仓库里 `examples/configs/*.yaml` 多数是这种 |

紧凑文件的好处是：**你不用把整副管线抄一遍，只写「在默认基础上改了什么」**，可读性和可维护性都好得多。仓库里的 [qwen3_omni_colocated_h100_bf16.yaml](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/examples/configs/qwen3_omni_colocated_h100_bf16.yaml) 就是典型——它只声明了 5 个 stage 各自的显存比例，其余拓扑全部继承自 `Qwen3OmniSpeechColocatedPipelineConfig` 的默认。

`stage_overrides` 的设计约束（来自 [manager.py:168-173](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L168-L173)）：**每个 stage 下只允许写 `runtime`，写别的键会报错**。这是有意的——拓扑结构（`next`/`terminal`/`factory` 等）应当由模型家族在代码里定义，配置文件只调「运行时预算」，不让用户在 YAML 里随手改坏拓扑。

#### 4.4.2 核心流程

紧凑覆盖的应用过程：

```text
from_file(yaml)
   │  config_cls(**data)            ← 先按默认 + 文件里的顶层字段重建完整配置
   ▼
full_config（含全部 stages）
   │  _apply_stage_overrides(config, stage_overrides)
   │     对每个 stage_name：
   │        - 校验 stage 存在
   │        - 校验只写了 runtime
   │        - _deep_merge_dict(stage.runtime, override.runtime)  ← 深度合并，不覆盖整块
   ▼
final_config
```

「深度合并」很关键：`_deep_merge_dict`（[manager.py:189-196](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L189-L196)）只会替换你写到的叶子字段，保留你没写的。所以你只写 `resources.total_gpu_memory_fraction`，不会把同一个 `runtime` 下的 `max_seq_len`、`sglang_server_args` 冲掉。

#### 4.4.3 源码精读

先看真实样例，[examples/configs/qwen3_omni_colocated_h100_bf16.yaml:1-32](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/examples/configs/qwen3_omni_colocated_h100_bf16.yaml#L1-L32)：

```yaml
config_cls: Qwen3OmniSpeechColocatedPipelineConfig
name: qwen3-omni-colocated-h100-bf16
model_path: Qwen/Qwen3-Omni-30B-A3B-Instruct

stage_overrides:
  thinker:
    runtime:
      resources:
        total_gpu_memory_fraction: 0.78
  talker_ar:
    runtime:
      resources:
        total_gpu_memory_fraction: 0.10
  # image_encoder / audio_encoder / code2wav 各 0.02 ...
```

这份文件**没有 `stages:` 列表**——它完全依赖 `config_cls` 提供拓扑，只在 `stage_overrides` 里调显存预算。五个 stage 加起来 `0.02+0.02+0.78+0.10+0.02 = 0.94`，留给框架与激活值的余量约 6%，这就是 colocated（同卡共存）部署的显存切分契约。

应用逻辑在 `_apply_stage_overrides`，[manager.py:147-186](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L147-L186)：

```python
for stage_name, override in stage_overrides.items():
    if stage_name not in stage_by_name:
        raise ValueError(f"stage_overrides references unknown stage {stage_name!r}")
    unsupported = sorted(set(override) - {"runtime"})   # 只允许 runtime
    if unsupported:
        raise ValueError(f"... supports only runtime overrides; got {unsupported}")
    ...
    stage["runtime"] = _deep_merge_dict(stage.get("runtime", {}), runtime_override)
```

两道护栏：① 引用的 stage 名必须真实存在（拼错会立刻报错）；② 只能覆盖 `runtime`（想改拓扑会被拒绝）。这保证了紧凑文件「只能调运行时、改不坏拓扑」。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：导出某模型的默认配置 → 修改一个 stage 的 `total_gpu_memory_fraction` → 说清楚这份覆盖会如何被应用。

**操作步骤**：

1. 导出默认配置（替换 `<模型路径>`）：
   ```bash
   sgl-omni config export --model-path <模型路径>
   ```
2. 打开生成的 `config_<name>.yaml`，找到 `stages` 里 `thinker`（或任一 stage）的：
   ```yaml
   runtime:
     resources:
       total_gpu_memory_fraction: null
   ```
3. 把它改成 `0.7`（或你想的值，必须在 `(0, 1]` 区间内，见 [schema.py:64-69](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L64-L69)）：
   ```yaml
   runtime:
     resources:
       total_gpu_memory_fraction: 0.7
   ```
4. （可选）用这份文件启动，并打印合并后的配置以核对：
   ```bash
   sgl-omni serve --config ./config_<name>.yaml --log-level debug 2>&1 | grep -A30 "Merged Configuration"
   ```

**需要观察的现象**：第 4 步打印的 `Merged Configuration` 里，`thinker.runtime.resources.total_gpu_memory_fraction` 应当是你写的 `0.7`。

**预期结果 / 该覆盖会如何被应用**（这是本题要回答的核心）：

- 由于你编辑的是**完整导出文件**，`from_file` 走的是「`config_cls(**data)` 直接重建」这条路（4.2.3）。你改的 `total_gpu_memory_fraction` 被直接写进了 `StageConfig.runtime.resources`，重建时通过 Pydantic 校验（值在 `(0, 1]` 才合法）。
- 这份 `PipelineConfig` 接着进入运行时准备（runtime prep）：`total_gpu_memory_fraction` 是一个**放置资源意图（placement-resource intent）**，文档明确它「在 TP 展开后，每个 rank 把这份预算贡献给它被分配的 GPU」（[schema.py:57-61](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L57-L61)）。放置规划据此决定哪些 stage 能同卡共存，随后 worker 在 import factory 后把它注入工厂（见 [config.md:166-168](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L166-L168)）。
- **优先级**：如果启动时还带了命令行点式覆盖（如 `--stages.thinker.runtime.resources.total_gpu_memory_fraction=0.8`），命令行会**最后**覆盖文件里的值（`merge_config` 在 `from_file` 之后执行）。

> 进阶写法：你也可以不改完整文件，而是新建一个**紧凑覆盖文件**（仿照 4.4.3 的 `stage_overrides` 写法），只写 `thinker.runtime.resources.total_gpu_memory_fraction: 0.7`，效果一样，且文件更小、更易维护。

**关于运行结果**：第 1～3 步的导出与编辑是确定性的；第 4 步能否真正启动取决于是否有对应 GPU/权重，标记为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果你在 `stage_overrides.thinker` 下写了 `factory: xxx`（想换工厂），会发生什么？

**参考答案**：会在 [manager.py:168-173](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L168-L173) 报错：`stage_overrides.thinker supports only runtime overrides; got unsupported keys ['factory']`。拓扑字段不允许在 YAML 里改。

**练习 2**：把 `thinker.runtime.resources.total_gpu_memory_fraction` 写成 `1.5` 会怎样？在哪一步炸？

**参考答案**：会在 `StageResourceConfig.model_post_init` 校验时抛 `must be in (0, 1]`（[schema.py:64-69](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L64-L69)）。由于覆盖应用后会用 `type(config)(**config_data)` 重建，这个错误在加载配置时（启动早期）就会暴露，不会拖到运行时。

**练习 3**：紧凑覆盖文件里，为什么 `_deep_merge_dict` 比直接整块替换 `runtime` 更安全？

**参考答案**：深度合并只覆盖你写到的叶子字段，保留模型默认里同属 `runtime` 的其他设置（如 `max_seq_len`、`sglang_server_args.mem_fraction_static`）。整块替换会把这些没写的字段抹掉，可能导致阶段丢配置。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**看 → 导 → 改 → 验**」的完整流程：

1. **看**：选一个本地可解析的模型，运行 `sgl-omni config view --model-path <模型路径>`，找到 `config_cls` 字段，记下它（对应 4.1 的解析结果）。
2. **导**：运行 `sgl-omni config export --model-path <模型路径> --output-path my.yaml`，得到完整配置文件（4.3）。
3. **改**：在 `my.yaml` 里挑两个 stage，分别设置不同的 `total_gpu_memory_fraction`，保证它们的和不超过 `1.0`（4.4）。再尝试在文件**末尾**追加一个 `stage_overrides:` 块，给第三个 stage 也设一个值——验证「完整导出 + 追加 stage_overrides」也能被 `from_file` 正确合并。
4. **验**：用 `sgl-omni serve --config my.yaml --log-level debug` 查看打印的 `Merged Configuration`（[serve.py:1306-1307](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1306-L1307)），核对三个 stage 的显存比例是否如你所写。

**思考题**：如果你在 `my.yaml` 里把某 stage 的 `total_gpu_memory_fraction` 之和算到了 `1.2`，配置加载阶段会报错吗？还是会在放置规划阶段才报错？带着这个问题去读 [config.md:183-185](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L183-L185) 关于「多进程组共享 GPU 需显式且不超 placement 上限」的描述。

> 提示：单个值的合法性（每个值 ∈ `(0, 1]`）在 Pydantic 校验时就会拦；但「同一 GPU 上多个 stage 之和是否超限」涉及放置规划，要等运行时准备阶段才会检查。

---

## 6. 本讲小结

- 配置是**模型定义**（提供 `PipelineConfig` 拓扑）与**模型无关运行时**（Scheduler/ModelRunner/传输层）之间的契约；改配置不改模型代码、也不改运行时代码。
- `resolve_config_cls_for_model_path` 用 `model_path` → HF `config.json` → `architecture` → 注册表，查出该用哪个配置类；这一步只需元数据，**不需权重、不需 GPU**。
- `ConfigManager` 统一管理三种来源（模型默认 / 配置文件 / 命令行点式覆盖），优先级是**命令行 > 文件 > 默认**，并用点式路径与 Pydantic 校验保证合并结果合法。
- `config view` 打印到终端、`config export` 落盘成 YAML，两者核心都是 `model_dump(mode="json")` + `yaml.dump`；导出文件靠自动写入的 `config_cls` 字段实现可回读。
- 仓库里的 YAML 多是**紧凑覆盖文件**：用 `config_cls` 继承拓扑，只在 `stage_overrides` 里调运行时；`stage_overrides` 每个阶段**只能写 `runtime`**，且用深度合并保留未覆盖字段。
- `total_gpu_memory_fraction` 是放置资源意图，约束每个 rank 的显存预算，最终由放置规划与 worker 注入工厂时生效。

## 7. 下一步学习建议

- 想深入「配置字段到底有哪些、拓扑/放置/终态如何由 stage 派生」，进入 **u2-l5 声明式配置 PipelineConfig 与 StageConfig**，它会逐字段拆解 `StageConfig` 与 `CommConfig`。
- 想看「配置被加载之后，运行时如何据此搭进程拓扑」，进入 **u3-l4 进程拓扑与多进程 Runner**（`mp_runner.py` / `topology.py`）。
- 想理解「模型家族如何把自己注册进注册表」，进入 **u5-l1 模型注册表与能力声明**，它会讲清 `import_pipeline_configs` 与 `EntryClass` 的自动扫描机制。
- 继续阅读源码：[sglang_omni/config/manager.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py)（合并与覆盖的全部细节）与 [docs/developer_reference/config.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md)（字段参考表）。
