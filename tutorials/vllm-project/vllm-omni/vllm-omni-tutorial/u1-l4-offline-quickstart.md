# 离线推理初体验：用 Omni 类生成图像

## 1. 本讲目标

学完本讲后，你应当能够：

- 用 `Omni(model=...).generate(prompt)` 跑通一次文生图（text-to-image）离线推理。
- 读懂 `generate` 的输入（prompt / 采样参数）与返回值 `OmniRequestOutput` 的结构，并能从返回值里正确取出图片。
- 运行官方示例脚本 `examples/offline_inference/text_to_image/text_to_image.py`，并理解它的核心调用链。
- 理解「多 prompt 自动批处理」：每条 prompt 是一个独立逻辑请求，运行时可能把兼容的请求合并进一次批量去噪。

本讲只关心**离线批处理推理**（offline batched inference），即「在 Python 进程里直接调用类生成结果」，不涉及起 HTTP 服务（那是下一讲 u1-l5 的内容）。

## 2. 前置知识

在开始前，建议你已经具备以下认知（来自 u1-l1 ~ u1-l3）：

- **vLLM-Omni 是「增量扩展」而非重写**：它在 vLLM 之上新增了对「非文本输出、非自回归结构（尤其 DiT 扩散模型）」的支持。
- **Diffusion 是最大的新增模块**：本讲用到的文生图模型（如 `Tongyi-MAI/Z-Image-Turbo`、`Qwen/Qwen-Image`）属于「DiT 为主」的单阶段扩散模型，可以直接用 `Omni` 类驱动。
- **`Omni` 是同步离线入口**，`AsyncOmni` 是异步入口；两者都继承自共享基类 `OmniBase`。它们通过 `vllm_omni/entrypoints/` 暴露，并被包的 `__init__.py` 懒加载（避免 import 时拖入重依赖）。

两个术语先解释清楚：

- **离线推理（offline inference）**：不对外提供服务，而是在脚本里直接 `import` 类、构造请求、拿结果。适合实验、评测、批量生成。
- **逻辑请求（logical request）**：你传进来的一条 prompt，在 vLLM-Omni 内部会被当作一个独立的请求来调度。多条 prompt = 多个逻辑请求，它们**可以**被运行时合并成一次批量计算（详见 4.4）。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| `docs/getting_started/quickstart.md` | 官方快速上手文档，给出最简的单 prompt 和多 prompt 离线代码片段。 |
| `examples/offline_inference/text_to_image/text_to_image.py` | 可运行的命令行示例脚本，封装了「解析参数 → 构造 prompt 与采样参数 → 生成 → 保存图片」的完整流程。 |
| `vllm_omni/entrypoints/omni.py` | 定义同步离线入口类 `Omni`，核心方法 `generate`。 |
| `vllm_omni/entrypoints/omni_base.py` | `Omni` 的基类 `OmniBase`，负责构造引擎 `AsyncOmniEngine`、维护请求状态等共享逻辑。 |
| `vllm_omni/outputs/__init__.py` | 定义统一返回结构 `OmniRequestOutput`（同时覆盖多阶段流水线与扩散两种模式）。 |
| `vllm_omni/inputs/data.py` | 定义 prompt 类型别名 `OmniPromptType` 与扩散采样参数 `OmniDiffusionSamplingParams`。 |

## 4. 核心概念与源码讲解

### 4.1 从 quickstart 开始：最简离线推理

#### 4.1.1 概念说明

最快理解一个推理框架的方式，是跑通它的「最短可运行示例」。vLLM-Omni 的 quickstart 文档把离线文生图浓缩成了 5 行代码：导入 `Omni` → 实例化模型 → 调用 `generate` → 从返回值取图片 → 保存。

这一段要建立两个直觉：

1. `Omni` 把「加载模型、初始化引擎、调度、去噪、解码」全部藏起来了，调用方只看到 `generate(prompt)`。
2. 即使是扩散模型，调用形式和 vLLM 的 `LLM.generate()` 几乎一样——这是 vLLM-Omni「与 vLLM 核心兼容」设计目标的具体体现。

#### 4.1.2 核心流程

单 prompt 离线推理的执行流程（伪代码）：

```text
1. from vllm_omni.entrypoints.omni import Omni   # 触发包初始化：version 检查 → patch → 注册 configs
2. omni = Omni(model="Tongyi-MAI/Z-Image-Turbo") # 下载权重 + 构造 AsyncOmniEngine + 加载 stage
3. outputs = omni.generate(prompt)                # 提交 1 个逻辑请求，阻塞等待结果
4. images = outputs[0].request_output.images      # 从返回值取 PIL 图片列表
5. images[0].save("coffee.png")                   # 落盘
```

第 2 步里，`Omni.__init__`（见 4.2）会构造一个 `AsyncOmniEngine`，后者为这个模型建立若干个 **stage（阶段）**。对于 Z-Image-Turbo 这种单阶段扩散模型，只有 1 个 diffusion stage。

#### 4.1.3 源码精读

官方 quickstart 的单 prompt 片段（[docs/getting_started/quickstart.md:L43-L52](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/getting_started/quickstart.md#L43-L52)）：

```python
from vllm_omni.entrypoints.omni import Omni

if __name__ == "__main__":
    omni = Omni(model="Tongyi-MAI/Z-Image-Turbo")
    prompt = "a cup of coffee on the table"
    outputs = omni.generate(prompt)
    images = outputs[0].request_output.images
    images[0].save("coffee.png")
```

要点解读：

- `outputs` 是一个 **列表**：`generate` 的返回类型是 `list[OmniRequestOutput]`（默认非生成器模式，见 4.2.3）。`outputs[0]` 就是这条 prompt 对应的结果。
- 取图片用 `outputs[0].request_output.images`：`request_output` 是底层引擎输出，扩散场景下它带 `.images`（PIL 图片列表）。注意 `OmniRequestOutput` 本身也有一个 `.images` 字段（见 4.3），两条路径都能拿到图片。
- `images[0].save(...)`：PIL 的 `Image` 对象自带 `save`，可直接写文件。

#### 4.1.4 代码实践

> **实践目标**：在已按 u1-l2 完成源码安装的环境里，跑通最简单的单图生成。

操作步骤：

1. 激活你用于 vLLM-Omni 的虚拟环境（`source .venv/bin/activate`）。
2. 把上面的 5 行代码存成 `my_first_gen.py`。
3. 运行：`python my_first_gen.py`。
4. 观察终端：首次运行会下载权重并打印引擎初始化日志（如 `Initializing with model ...`、`Initialized with 1 stages ...`），随后生成并保存 `coffee.png`。

需要观察的现象与预期结果：

- 终端出现 stage 初始化相关日志，且最终没有抛异常。
- 当前目录下出现 `coffee.png`，打开是一张「桌上咖啡杯」的图。
- 首次运行较慢（含下载与模型加载），再次运行因权重已缓存而更快。

> 若没有 GPU 或显存不足，本步骤需在合适的机器上完成；本地无法验证时记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：把模型换成 `Qwen/Qwen-Image`，把 prompt 改成 `"a red fox in snow"`，预期 `outputs[0]` 的结构会变吗？

> **参考答案**：不会。`generate` 的返回值始终是 `list[OmniRequestOutput]`，结构由 `OmniRequestOutput` 定义，与具体模型无关。改变的只是 `images` 里图片的内容。

**练习 2**：为什么 quickstart 没有显式调用 `omni.close()` 也能正常退出？

> **参考答案**：非生成器模式下 `generate` 不主动关闭引擎，但 `OmniBase.__init__` 注册了一个 weakref 终结器（`weakref.finalize`），当 `Omni` 对象被垃圾回收时会 best-effort 调用 `engine.shutdown()`（见 `omni_base.py` 中 `_weak_shutdown_engine`）。短脚本进程结束时对象随之回收，从而完成清理。

---

### 4.2 `Omni` 类与 `generate` 的输入

#### 4.2.1 概念说明

`Omni` 是**同步**离线入口，定义在 [vllm_omni/entrypoints/omni.py:L25](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni.py#L25)，继承自 `OmniBase`（[vllm_omni/entrypoints/omni_base.py:L103](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni_base.py#L103)）。它的核心方法 `generate` 有两个关键设计：

1. **prompt 既可以是一条，也可以是一组**：`OmniPromptType | Sequence[OmniPromptType]`。传字符串就是一条；传列表就是多条独立逻辑请求。
2. **采样参数可以按 stage 给**：`sampling_params_list` 是「每个 stage 一份」的列表，长度必须等于 stage 数量。单阶段扩散模型只需要一份 `OmniDiffusionSamplingParams`。

`OmniPromptType`（[vllm_omni/inputs/data.py:L151](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L151)）是多种 prompt 形式的联合类型：既兼容 vLLM 原生的 `PromptType`（字符串、token id 列表、`TextPrompt`/`TokensPrompt`/`EmbedsPrompt`），也支持 Omni 扩展的 `OmniTextPrompt`/`OmniTokensPrompt`/`OmniEmbedsPrompt`/`OmniCustomPrompt`。这些扩展类型在原生基础上增加了 `prompt_embeds`（阶段间传递的嵌入）、`additional_information`（附加信息）、`modalities`（任务类型标记）等字段——本讲用到的文生图主要是字符串 prompt 或 `OmniTextPrompt`。

#### 4.2.2 核心流程

`generate` 内部（非生成器模式）的执行流程：

```text
generate(prompts, sampling_params_list=None, py_generator=False)
  ├─ 规范化 sampling_params_list（缺失则用默认值，长度必须 == stage 数）
  ├─ 把 LLM(非扩散) stage 的 output_kind 强制为 FINAL_ONLY（_maybe_force_final_only_for_llm_stages）
  ├─ 调用 _run_generation(prompts, ...)：
  │     ├─ 把 prompts 规范成 list；若为空直接返回
  │     ├─ 为每条 prompt 生成唯一 request_id（"{i}_{uuid}"）
  │     ├─ 逐条调用 engine.add_request(...) 提交
  │     ├─ while 还有未完成请求：
  │     │     msg = engine.try_get_output()      # 从输出队列拉一条消息
  │     │     解析消息 → 处理 metrics/error → 组装 OmniRequestOutput
  │     │     若该请求 finished，则从 active_reqs 移除
  │     └─ 生成器逐个 yield 结果
  └─ list(...) 收集为列表返回（非生成器模式）
```

关键点：`generate` 是**阻塞**的——它在 `while active_reqs` 循环里反复调用 `engine.try_get_output()`，直到所有提交的请求都完成。

#### 4.2.3 源码精读

`generate` 的签名与重载（[vllm_omni/entrypoints/omni.py:L51-L78](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni.py#L51-L78)）。注意两个 `@overload`：传 `py_generator=True` 返回生成器，否则返回列表：

```python
def generate(
    self,
    prompts: OmniPromptType | Sequence[OmniPromptType],
    sampling_params_list: OmniSamplingParams | Sequence[OmniSamplingParams] | None = None,
    *,
    py_generator: bool = False,
    use_tqdm: bool | Callable[..., tqdm] = True,
) -> Generator[OmniRequestOutput, None, None] | list[OmniRequestOutput]:
```

实现体的分支（[vllm_omni/entrypoints/omni.py:L87-L94](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni.py#L87-L94)）——默认走 `list(...)`：

```python
if py_generator:
    return self._run_generation_with_generator(prompts, sampling_params_list, use_tqdm)
return list(self._run_generation(prompts, sampling_params_list, use_tqdm))
```

请求提交与轮询主循环（[vllm_omni/entrypoints/omni.py:L125-L171](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni.py#L125-L171)），关键三处：

```python
request_ids = [f"{i}_{uuid.uuid4()}" for i in range(len(request_prompts))]  # 每条 prompt 一个 id
...
self.engine.add_request(                       # 逐条提交
    request_id=req_id, prompt=prompt,
    sampling_params_list=req_sp_list,
    final_stage_id=final_stage_id,
    final_output_stage_ids=final_output_stage_ids,
)
...
active_reqs = set(request_ids)
while active_reqs:
    msg = self.engine.try_get_output()          # 阻塞式拉取下一条输出
```

> 注意 `request_id` 的格式 `{i}_{uuid}`：第 0 条 prompt 的 id 以 `0_` 开头。这能帮助你日志里把输出对应回输入的 prompt。

#### 4.2.4 代码实践

> **实践目标**：用源码阅读的方式验证「每条 prompt = 一个 request_id」，并理解 `generate` 的阻塞行为。

操作步骤：

1. 打开 [vllm_omni/entrypoints/omni.py:L125](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni.py#L125)，确认 `request_ids` 列表长度等于 `len(request_prompts)`。
2. 在 [omni.py:L153](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni.py#L153) 的 `self.engine.add_request(...)` 上方临时加一行日志（**示例代码**，仅用于本地观察，勿提交）：

   ```python
   logger.info("[debug] submitting request_id=%s", req_id)
   ```

3. 用一个含 3 条 prompt 的列表调用 `omni.generate(prompts)`，观察日志中 3 个不同 `request_id` 依次被提交，然后程序阻塞直到全部完成。

需要观察的现象与预期结果：日志打印 3 行 `submitting request_id=0_.. / 1_.. / 2_..`；程序在打印完所有「已提交」后才陆续产出结果，体现「先全部提交、再统一轮询」的模式。

> 本步骤需在能加载模型的机器上运行；仅做静态阅读时记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `py_generator=True` 传给 `generate`，返回类型会变成什么？它和默认模式在「何时关闭引擎」上有何区别？

> **参考答案**：返回一个生成器 `Generator[OmniRequestOutput]`。区别在于：生成器模式走 `_run_generation_with_generator`，其 `finally` 里会调用 `self.close()`（[omni.py:L105-L106](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni.py#L105-L106)）；默认 `list(...)` 模式不主动关闭，依赖 weakref 终结器在对象回收时清理。

**练习 2**：对一个 2 阶段模型，如果你只传 1 个采样参数对象（而不是长度为 2 的列表），会发生什么？

> **参考答案**：`OmniBase.resolve_sampling_params_list`（[omni_base.py:L269-L288](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni_base.py#L269-L288)）会校验长度。多阶段模型传单个参数会抛 `ValueError: Expected {N} sampling params, got a single sampling params object`。单阶段模型（`num_stages == 1`）才会被自动包成 `[params]`。

---

### 4.3 返回值结构：`OmniRequestOutput`

#### 4.3.1 概念说明

`generate` 返回的是 `OmniRequestOutput`（[vllm_omni/outputs/__init__.py:L75](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L75)）。它是 vLLM-Omni 的**统一输出容器**，设计目标是用一个类同时描述两种模式：

- **扩散模式（diffusion mode）**：产出 PIL 图片、latents、metrics。
- **多阶段流水线模式（pipeline mode）**：产出某个 stage 的 `request_output`、`stage_id` 等。

这样无论你是文生图（单阶段扩散）还是 Qwen-Omni（多阶段），上层代码都可以用同一套数据结构。文生图场景下，我们主要关心 `images` 和 `request_output.images`。

#### 4.3.2 核心流程

`OmniRequestOutput` 的两种构造入口对应两种模式：

```text
扩散模式：   OmniRequestOutput.from_diffusion(images=[...], prompt=..., final_output_type="image")
流水线模式： OmniRequestOutput.from_pipeline(stage_id, final_output_type, request_output)
```

而在 `Omni.generate` 的实际路径里，`OmniBase._process_single_result` 会**直接构造** `OmniRequestOutput`（不走上面两个类方法），并同时填充 `request_output`（=引擎输出）和 `images`（当 `output_type == "image"` 时从引擎输出里取出）。这就是为什么取图片有两种等价写法。

#### 4.3.3 源码精读

字段定义（[vllm_omni/outputs/__init__.py:L95-L125](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L95-L125)），文生图相关的关键字段：

```python
request_id: str = ""
finished: bool = True

stage_id: int | None = None
final_output_type: str = "text"
request_output: RequestOutput | None = None   # 底层引擎输出

images: list[Image.Image] = field(default_factory=list)  # 直接的图片列表
prompt: OmniPromptType | None = None
latents: torch.Tensor | None = None
metrics: dict[str, Any] = field(default_factory=dict)
```

两个有用的判断属性（[vllm_omni/outputs/__init__.py:L332-L339](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L332-L339)）：

```python
@property
def is_diffusion_output(self) -> bool:
    return len(self.images) > 0 or self.final_output_type == "image"

@property
def is_pipeline_output(self) -> bool:
    return self.stage_id is not None and self.request_output is not None
```

以及便捷的数量属性（[vllm_omni/outputs/__init__.py:L274-L277](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L274-L277)）：`num_images` 返回 `len(self.images)`。

> 小贴士：quickstart 用 `outputs[0].request_output.images`；你也可以直接用 `outputs[0].images` 或 `outputs[0].num_images`。当 `final_output_type == "image"` 时二者一致。官方示例脚本为了让各种模型都能取到图，做了多层兜底（见 4.4.3）。

#### 4.3.4 代码实践

> **实践目标**：用 `__repr__` 直观看到一个扩散输出的内部结构，加深对字段的印象。

操作步骤（**示例代码**）：

```python
from vllm_omni.entrypoints.omni import Omni

omni = Omni(model="Tongyi-MAI/Z-Image-Turbo")
outputs = omni.generate("a cup of coffee on the table")
out = outputs[0]

print(repr(out))                 # 看 OmniRequestOutput 的 repr（图片以数量显示，避免刷屏）
print("is_diffusion_output:", out.is_diffusion_output)
print("num_images:", out.num_images)
print("final_output_type:", out.final_output_type)
```

需要观察的现象与预期结果：

- `repr(out)` 中 `images=[1 PIL Images]`、`final_output_type='image'`。
- `is_diffusion_output` 为 `True`，`num_images` 为 1。

> 需在能加载模型的机器运行；仅静态阅读时记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`OmniRequestOutput` 为什么要把 `images` 和 `request_output` 都保留？只用一个不行吗？

> **参考答案**：为了「一个类覆盖两种模式」。流水线模式下图片/文本在 `request_output` 内部（`request_output` 可能又嵌套 `OmniRequestOutput`，需 `unwrap()`）；扩散模式下为了访问方便，把图片直接复制到顶层 `images`。保留两者让上层代码可以按场景选择最直接的访问路径，也方便 `unwrap_result()` 这类通用解包工具（[outputs/__init__.py:L369-L405](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L369-L405)）。

**练习 2**：`final_output_type` 在文生图场景下通常是什么值？它由谁决定？

> **参考答案**：通常是 `"image"`。它由该 stage 的元数据 `stage_meta.final_output_type` 决定，并由引擎输出 `engine_outputs.final_output_type` 覆盖（见 `omni_base._process_single_result`）。

---

### 4.4 多 prompt 自动批处理与 `text_to_image.py` 调用链

#### 4.4.1 概念说明

quickstart 的多 prompt 示例（[docs/getting_started/quickstart.md:L63-L86](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/getting_started/quickstart.md#L63-L86)）把一个 prompt 列表传给 `generate`，然后用双层循环把「每条 prompt 的每张图」分别存盘：

```python
prompts = ["a cup of coffee on a table", "a toy dinosaur on a sandy beach", "a fox waking up in bed and yawning"]
omni_outputs = omni.generate(prompts)
for i_prompt, prompt_output in enumerate(omni_outputs):
    for i_image, image in enumerate(prompt_output.request_output.images):
        image.save(f"p{i_prompt}-img{i_image}.jpg")
```

文档紧接着用一个 info 框点出本讲的**核心机制**（[docs/getting_started/quickstart.md:L57-L62](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/getting_started/quickstart.md#L57-L62)）：

> For diffusion pipelines, each prompt becomes a separate logical request. The runtime may automatically batch compatible in-flight requests through the scheduler and runner.

也就是说：

- 你传 N 条 prompt → 内部生成 N 个逻辑请求（N 个 `request_id`）。
- 调度器（scheduler）和 runner **可能**把其中「兼容」的、同时在飞的请求，合并进**一次** `pipeline.forward(batch)`，从而提升吞吐。
- 「自动批处理」是运行时根据兼容性与并发上限（如 `max_num_seqs`、`request_batch_max_wait_ms`）决定的，**不保证**一定合并（详见 `docs/user_guide/diffusion/request_batching.md`，后续 u7-l5 会深入）。

#### 4.4.2 核心流程

`text_to_image.py` 是一个**命令行脚本**，把上述能力封装成带参数的可运行程序。它的调用链（[examples/offline_inference/text_to_image/text_to_image.py:L354-L577](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/text_to_image/text_to_image.py#L354-L577)）：

```text
main()
 ├─ parse_args()                                  # 解析 --model/--prompt/--seed/--num-inference-steps 等
 ├─ 构造 cache_config（tea_cache / cache_dit）     # 可选加速
 ├─ 组装 omni_kwargs 字典
 ├─ omni = Omni(**omni_kwargs)                    # 实例化（含 mode="text-to-image"）
 ├─ build_text_to_image_prompt(...)  → prompt_dict          # 把 prompt/负向 prompt/宽高打包成 OmniTextPrompt
 ├─ OmniDiffusionSamplingParams(height, width, seed, ...)    # 构造扩散采样参数
 ├─ 采样参数塞进 sampling_params_list（按 stage）
 ├─ outputs = omni.generate(prompt_dict, sampling_params_list=...)  # 单 prompt 生成
 └─ 从 outputs 多层兜底取 images → 保存到 --output 路径
```

注意：这个脚本本身**只接受单个 `--prompt`**（[text_to_image.py:L84](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/text_to_image/text_to_image.py#L84)），即一次运行只生成一张图。要体会「多 prompt 自动批处理」，应改用 4.4.1 的列表写法（见综合实践）。

#### 4.4.3 源码精读

脚本实例化 `Omni` 时，把大量可选能力（缓存、量化、并行、offload）都收进 `omni_kwargs`（[text_to_image.py:L409-L449](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/text_to_image/text_to_image.py#L409-L449)），关键字段之一是 `mode="text-to-image"`：

```python
omni_kwargs = {
    "model": args.model,
    ...
    "mode": "text-to-image",
    ...
}
omni = Omni(**omni_kwargs)
```

构造扩散采样参数（[text_to_image.py:L502-L512](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/text_to_image/text_to_image.py#L502-L512)）。`OmniDiffusionSamplingParams`（定义见 [inputs/data.py:L196](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L196)）集中了扩散推理需要的尺寸、步数、CFG、随机种子等：

```python
diffusion_params = OmniDiffusionSamplingParams(
    height=args.height, width=args.width,
    seed=args.seed, generator=generator,
    true_cfg_scale=args.cfg_scale, guidance_scale=args.guidance_scale,
    num_inference_steps=args.num_inference_steps,
    num_outputs_per_prompt=args.num_images_per_prompt,
)
```

提交生成（[text_to_image.py:L577](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/text_to_image/text_to_image.py#L577)）：

```python
outputs = omni.generate(prompt_dict, sampling_params_list=sampling_params_list)
```

取图片的**多层兜底**逻辑（[text_to_image.py:L607-L621](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/text_to_image/text_to_image.py#L607-L621)）值得学习——它依次尝试 `output.images`、`output.request_output.images`，最后再回退到 `extract_images_from_outputs(outputs)`：

```python
for output in outputs:
    images = getattr(output, "images", None)
    if images: break
    req_out = getattr(output, "request_output", None)
    images = getattr(req_out, "images", None) if req_out is not None else None
    if images: break
if not images:
    images = extract_images_from_outputs(outputs)   # 最终兜底
```

> 这种「先顶层字段、再内层字段、再专用工具函数」的取值顺序，正是因为不同模型/模式把图片放在不同位置。写自己的离线脚本时可以照搬。

#### 4.4.4 代码实践

> **实践目标**：运行官方脚本生成单张图，再用列表写法生成 3 张图，对照体会「多 prompt 自动批处理」。

操作步骤：

1. **单图（验证脚本与安装）**：直接运行脚本（默认用 `Qwen/Qwen-Image`，可按需 `--model` 切换）。

   ```bash
   python examples/offline_inference/text_to_image/text_to_image.py \
     --model Tongyi-MAI/Z-Image-Turbo \
     --prompt "a toy dinosaur on a sandy beach" \
     --output beach.png
   ```

   预期：终端打印生成耗时，当前目录出现 `beach.png`。

2. **多 prompt 自动批处理（核心实践）**：注意该脚本只接受单个 `--prompt`，因此请用 quickstart 的列表写法。新建 `multi_prompts.py`（**示例代码**）：

   ```python
   from vllm_omni.entrypoints.omni import Omni

   omni = Omni(model="Tongyi-MAI/Z-Image-Turbo")
   prompts = [
       "a cup of coffee on a table",
       "a toy dinosaur on a sandy beach",
       "a fox waking up in bed and yawning",
   ]
   outs = omni.generate(prompts)
   for i, o in enumerate(outs):
       imgs = o.images or o.request_output.images
       for j, img in enumerate(imgs):
           img.save(f"p{i}-img{j}.jpg")
           print("saved", f"p{i}-img{j}.jpg")
   ```

3. 运行 `python multi_prompts.py`，预期得到 `p0-img0.jpg`、`p1-img0.jpg`、`p2-img0.jpg` 三张图。

需要观察的现象：

- 三张图内容与三条 prompt 分别对应；`outs` 长度为 3，`request_id` 形如 `0_.. / 1_.. / 2_..`。
- 是否真的发生「自动批处理」（多条 prompt 合并进一次去噪）取决于调度器的兼容性判断与 `max_num_seqs` 等参数；若想确认，可加上 `log_stats=True`（或开启 diffusion pipeline profiler）观察吞吐与步数。**批处理是否触发属运行时行为，记为待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：quickstart 多 prompt 例子用 `prompt_output.request_output.images`，而 4.4.4 的示例代码写成 `o.images or o.request_output.images`。为什么后者更稳？

> **参考答案**：`o.images`（顶层）在 `final_output_type == "image"` 时被填充；某些路径或模型可能只在 `request_output.images` 里放图片。用 `o.images or o.request_output.images`（以及官方脚本的 `extract_images_from_outputs` 兜底）能覆盖更多模型，避免取到空列表。

**练习 2**：`max_num_seqs=1` 和 `max_num_seqs=4` 在多 prompt 场景下的主要区别是什么？

> **参考答案**：`max_num_seqs` 限制同一次批量去噪里同时处理的请求数。`=1` 时请求基本串行（一次只去噪一条），吞吐低但显存占用小；`=4` 时调度器可把最多 4 条兼容请求合并进一次 `pipeline.forward(batch)`，提升吞吐，代价是更高显存。是否合并还受请求到达时机与 `request_batch_max_wait_ms` 影响（详见 `docs/user_guide/diffusion/request_batching.md`）。

## 5. 综合实践

把本讲的知识串起来，完成一个小任务：**写一个「3 条 prompt 离线生成 + 结构化打印」的脚本，并回答三个问题。**

要求：

1. 用 `OmniDiffusionSamplingParams` 显式指定 `height=1024`、`width=1024`、`num_inference_steps=20`、`seed=42`（参考 [text_to_image.py:L502-L512](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/text_to_image/text_to_image.py#L502-L512)）。
2. 用 3 条不同 prompt 调用 `omni.generate(prompts, sampling_params_list=[diffusion_params])`。
3. 对每条结果，打印 `request_id`、`final_output_type`、`num_images`，并把图片保存为 `task_p{i}.png`。

完成后回答：

- (a) `outs` 的长度是否等于 prompt 数？每个 `request_id` 的前缀分别是什么？
- (b) 若把 `num_inference_steps` 从 20 改到 50，单张图的质量与耗时分别如何变化（定性即可）？
- (c) 为什么说这 3 条 prompt 是「3 个独立逻辑请求」，而运行时「可能」把它们批处理？

> 预期：(a) 长度为 3，`request_id` 前缀依次为 `0_ / 1_ / 2_`（见 [omni.py:L125](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni.py#L125)）。(b) 步数更多通常画质更细但更慢。(c) 见 4.4.1：每条 prompt → 一个 `request_id`；是否合并由调度器按兼容性与并发上限决定。

## 6. 本讲小结

- 离线推理用同步入口 `Omni`（继承 `OmniBase`），核心方法 `generate(prompts, sampling_params_list=None, py_generator=False)`。
- `generate` 返回 `list[OmniRequestOutput]`（默认）；文生图场景下每条 prompt 对应一个结果，图片可从 `images` 或 `request_output.images` 取得。
- `OmniRequestOutput` 是统一输出容器，用 `final_output_type`/`images`/`request_output` 等字段同时描述扩散与多阶段两种模式；`is_diffusion_output` / `num_images` 是常用便捷属性。
- `text_to_image.py` 是可运行的命令行示例，封装了「参数解析 → 构造 prompt 与 `OmniDiffusionSamplingParams` → 生成 → 多层兜底取图 → 保存」的完整调用链，但脚本本身只接受单个 `--prompt`。
- 多 prompt 离线推理：每条 prompt 是一个独立逻辑请求（独立 `request_id`），运行时**可能**把兼容的请求自动批处理进一次去噪，是否触发取决于调度器与 `max_num_seqs` 等参数。

## 7. 下一步学习建议

- 想了解「多 prompt 自动批处理」的底层机制，可先读 `docs/user_guide/diffusion/request_batching.md`，后续进阶层 **u5（Diffusion 模块）** 与 **u7-l5（Diffusion 批处理）** 会系统讲解 scheduler/executor。
- 想把模型跑成 HTTP 服务、用 `curl` 调用，请进入下一讲 **u1-l5 在线服务初体验：`vllm serve --omni` 与 OpenAI 兼容 API**。
- 想理解 `Omni.__init__` 是如何把模型变成若干 stage 的，可在学完 u1-l5 后阅读 **u2（核心抽象）**，尤其是 u2-l2（配置体系）与 u3-l1（AsyncOmni 架构）。
