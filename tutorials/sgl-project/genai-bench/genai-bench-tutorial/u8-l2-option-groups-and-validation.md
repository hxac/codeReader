# CLI 选项分组与校验机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `benchmark` 命令那几十个 `--xxx` 选项是如何被「分组」拼装出来的，以及为什么必须分组。
- 写出一个合法的 click `callback(ctx, param, value)` 校验回调，并用它做三件事：转换值、共享状态、拒绝非法输入。
- 解释「签名顺序」与 `is_eager=True` 两条规则如何决定参数的处理先后，从而让一个回调能读到另一个回调的结果。
- 看懂 `validate_prefix_options` 这种「解析完成后再整体校验」的第三种校验风格，并区分 `click.BadParameter` 与 `click.UsageError` 的用法。
- 独立构造若干会触发 `--prefix-len` / `--prefix-ratio` 报错的参数组合，并解释每条错误的触发条件。

本讲是 U8 专家层的第二篇，承接 u8-l1 的 benchmark 主流程编排：u8-l1 讲「主流程怎么跑」，本讲退一步讲「主流程开跑之前，那几十个命令行参数是怎么被组织、被校验、被联动起来的」。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个 click 概念。它们是本讲的全部地基。

**（1）装饰器叠加 = 给函数逐层「贴」参数。**
click 用 `@click.option(...)` 给一个函数加参数。多个 `@click.option` 从下往上叠加，最终这个函数就拥有一长串参数。本讲大量出现「装饰器工厂函数」，它的本质就是「把一摞 `@click.option` 打包成一个可复用的装饰器」。

**（2）回调 callback = 解析时的小钩子。**
每个 `click.option` 可以挂一个 `callback`，签名固定为 `callback(ctx, param, value)`：

- `ctx`：click 上下文，是「整个命令解析过程的钱包」，里面装着已解析的参数（`ctx.params`）和跨回调共享的对象袋（`ctx.obj`）。
- `param`：当前这个选项自己（`param.name` 是选项名）。
- `value`：用户传入、经过类型转换后的值。
- **返回值**：回调可以返回一个新值替换原值，也可以原样返回。

回调在「参数被解析的那一刻」同步执行，比命令函数体早得多。这就是「回调式校验」。

**（3）两类报错异常。**

| 异常 | 含义 | 典型场景 |
| --- | --- | --- |
| `click.BadParameter` | 「这个参数的值不对」，错误指向单个选项 | 回调里发现某个 `--xxx` 非法 |
| `click.UsageError` | 「整条命令的用法不对」，错误属于整个命令 | 多个参数合在一起才暴露的矛盾 |

记住这个对照表，后面读源码会反复用到。

> 如果你还不熟悉 click 的 group / option / pass_context，请先读 u1-l4「CLI 入口与三大命令」。本讲默认你已经知道 `@click.group()`、`@click.command()`、`ctx.obj` 是什么。

## 3. 本讲源码地图

本讲只涉及两个核心源码文件，外加它们在主流程里的调用点：

| 文件 | 作用 |
| --- | --- |
| [genai_bench/cli/option_groups.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py) | 把 `benchmark` 的几十个选项按主题拆成 10 个「选项组装饰器」（如 `api_options`、`sampling_options`）。 |
| [genai_bench/cli/validation.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py) | 实现挂在选项上的各类校验回调（如 `validate_task`、`validate_iteration_params`）以及解析完成后的整体校验 `validate_prefix_options`。 |
| [genai_bench/cli/cli.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py) | `benchmark` 命令本体，用装饰器把各选项组叠在一起，并在函数体里显式调用 `validate_prefix_options`。 |
| [tests/cli/test_validation.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/cli/test_validation.py) | 前缀校验的单元测试，是本讲代码实践的直接依据。 |

一张关系图：

```
option_groups.py                    validation.py
┌────────────────────┐              ┌──────────────────────────┐
│ api_options        │─callback────▶│ validate_api_backend      │
│ sampling_options   │─callback────▶│ validate_task             │
│ experiment_options │─callback────▶│ validate_iteration_params │
│ ...                │              │ validate_prefix_options   │ (后置)
└────────────────────┘              └──────────────────────────┘
         │                                       ▲
         │ @api_options @sampling_options ...    │ 显式调用
         ▼                                       │
      cli.py: benchmark(...)  ───────────────────┘
```

---

## 4. 核心概念与源码讲解

### 4.1 选项分组：把几十个选项装进可复用的装饰器

#### 4.1.1 概念说明

`benchmark` 命令最终需要接收 **约 70 个参数**（API 配置、各云厂商认证、采样、存储、实验……）。如果把这 70 个 `@click.option` 全部直接堆在 `benchmark` 函数上方，文件会变成无法维护的一堵墙。

genai-bench 的做法是「按主题拆组」：把相关的若干选项收进一个**普通函数**（如 `api_options`、`sampling_options`），这个函数接收一个函数 `func`，内部用 `func = click.option(...)(func)` 反复给它贴选项，最后 `return func`。它本身就是一个「装饰器」。

于是 `benchmark` 顶部只需写 10 行分组装饰器，而不是 70 行零散选项：

```python
@click.command(context_settings={"show_default": True})
@api_options
@model_auth_options
@oci_auth_options
@server_options
@experiment_options
@sampling_options
@distributed_locust_options
@object_storage_options
@storage_auth_options
@metrics_options
@click.pass_context
def benchmark(ctx, api_backend, api_base, ...):
```

这带来三个好处：

1. **可读性**：一眼看出参数分了哪几类。
2. **可复用**：未来若有第二个命令也需要 API 配置，直接 `@api_options` 即可。
3. **关注点分离**：每个分组文件内只关心自己那几个选项。

#### 4.1.2 核心流程

一个选项组装饰器的执行流程：

1. click 从下往上应用装饰器，所以最靠近函数体的 `@metrics_options` 先执行。
2. 每个分组函数内部，按「从上到下」的源码顺序执行多条 `func = click.option(...)(func)`。
3. 关键技巧：**选项组内部以「倒序」起作用**。源码里写在最下面的 `click.option`，反而是最先被注册、最终在函数签名里排最前的参数。

源码顶部有一条注释专门提醒这一点：

> [option_groups.py:L21-L23](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L21-L23) ——「新增选项请加在函数顶部，因为装饰器按倒序工作」。

这条「倒序」规则直接决定了跨回调的依赖顺序（见 4.3），所以非常重要。

#### 4.1.3 源码精读

以 `api_options` 为例（[option_groups.py:L24-L104](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L24-L104)）。注意源码顺序与最终签名顺序的对应：

```python
def api_options(func):
    func = click.option("--additional-request-params", ...)(func)  # 源码第 1 个 → 签名最后
    func = click.option("--api-model-name", ...)(func)
    func = click.option("--task", ..., callback=validate_task)(func)
    func = click.option("--api-key", ..., callback=validate_api_key)(func)
    func = click.option("--api-base", ...)(func)
    func = click.option("--api-backend", ..., callback=validate_api_backend)(func)  # 源码最后 → 签名最前
    return func
```

由于倒序，最终 `benchmark` 签名里 API 相关参数的实际顺序是：

```
api_backend, api_base, api_key, api_model_name, ..., task, ..., additional_request_params
```

这与 [cli.py:L74-L89](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L74-L89) 里 `benchmark` 的形参顺序完全一致——`api_backend` 排第一，`task` 排后面。**正是这个顺序，保证了 `--api-backend` 的回调先于 `--task` 的回调执行**，4.3 节会展开。

整张分组全景表（10 个组）：

| 分组装饰器 | 选项行号 | 涵盖的典型选项 |
| --- | --- | --- |
| `api_options` | L24–L104 | `--api-backend` `--task` `--api-key` `--api-model-name` |
| `model_auth_options` | L108–L237 | 各云模型认证（AWS/Azure/GCP 等） |
| `oci_auth_options` | L241–L292 | OCI 专属认证 |
| `sampling_options` | L296–L352 | `--dataset-*` `--prefix-len` `--prefix-ratio` |
| `server_options` | L356–L405 | `--server-gpu-*` `--server-engine` |
| `experiment_options` | L409–L646 | `--traffic-scenario` `--num-concurrency` `--iteration-type` 等 |
| `distributed_locust_options` | L649–L673 | `--num-workers` `--master-port` |
| `object_storage_options` | L842–L856 | `--upload-results` `--namespace` |
| `storage_auth_options` | L677–L838 | 各云存储认证 |
| `metrics_options` | L860–L869 | `--metrics-refresh-interval` |

#### 4.1.4 代码实践

**实践目标**：亲手验证「分组装饰器 = 一摞 click.option」的事实，并确认最终签名顺序。

**操作步骤**：

1. 在仓库根目录启动 Python：
   ```bash
   python -c "from genai_bench.cli.cli import benchmark; \
import inspect; print([p for p in inspect.signature(benchmark.callback).parameters][:10])"
   ```
2. 阅读本节给出的 `api_options` 源码，对照打印出的前 10 个参数名，确认 `api_backend` 排在 `task` 之前。

**需要观察的现象**：打印出的参数列表以 `ctx, api_backend, api_base, api_key, api_model_name, model, model_tokenizer, task, ...` 开头，顺序与本节描述一致。

**预期结果**：你会清楚看到「源码里写在最后的 `--api-backend`，在签名里排到了最前」，从而理解「倒序」规则的物质后果。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `--task` 选项从 `api_options` 的当前位置挪到该函数源码的最顶部（`--additional-request-params` 之前），会发生什么？

**参考答案**：倒序规则下，`--task` 会变成签名里最靠后的 API 参数，于是它的回调 `validate_task` 会在 `--api-backend` 之后执行——此时仍能读到 `ctx.obj`（因为 `ctx.obj` 不依赖处理顺序，只要 `validate_api_backend` 先跑过即可）。但如果改的是 `validate_api_key`（它读的是 `ctx.params["api_backend"]`，依赖处理顺序），把 `--api-key` 挪到 `--api-backend` 之前就会让 `ctx.params.get("api_backend")` 取到 `None`，从而抛出 "api_backend must be specified before api_key"。结论：**依赖 `ctx.params` 的跨参数校验对顺序敏感，依赖 `ctx.obj` 的相对宽松**。

**练习 2**：`benchmark` 函数体有近 80 个形参，为什么不需要手写 `**kwargs`？

**参考答案**：因为每个选项组装饰器注册的选项名，与 `benchmark` 形参一一对应（click 把 `--api-backend` 映射为 `api_backend` 关键字传入）。只要形参名和选项名（下划线形式）匹配，click 就会按关键字传入，顺序无关。这也是 u1-l4 提到的「click 按选项名以关键字参数传入、顺序无关」。

---

### 4.2 回调式校验：`callback(ctx, param, value)` 的三种用法

#### 4.2.1 概念说明

「回调式校验」指：把校验逻辑写成一个函数，挂在 `click.option(callback=...)` 上，在参数被解析的瞬间执行。一个回调可以同时承担三种职责，genai-bench 三种都用到了：

1. **转换值**：把字符串解析成结构化对象，返回新值。
2. **共享状态**：把解析结果写进 `ctx.obj` 或 `ctx.params`，供后面的回调或命令体使用。
3. **拒绝非法值**：发现问题时 `raise click.BadParameter(...)`，立刻中断。

理解回调的关键是：**回调既能读「钱包」，也能改「钱包」**。

#### 4.2.2 核心流程

click 解析参数时，对每个带回调的选项：

```
用户输入 → 类型转换(如 IntRange/Choice) → callback(ctx, param, value)
                                                │
                                 ┌──────────────┼──────────────┐
                                 ▼              ▼              ▼
                            返回新值        写 ctx.obj/params   raise BadParameter
                          (替换 value)      (共享给后续)        (中断解析)
```

注意 `ctx.params` 在解析过程中是**渐进填充**的：只有「已经被处理过的」参数才会出现在里面。这引出 4.3 节的顺序问题。

#### 4.2.3 源码精读

**用法一：转换值。** `set_model_from_tokenizer` 把 `--model` 缺省值从 `--model-tokenizer` 推断出来（取 HuggingFace ID 的最后一段）：

> [validation.py:L355-L360](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L355-L360)
```python
def set_model_from_tokenizer(ctx, param, value):
    model_tokenizer = ctx.params.get("model_tokenizer")
    return value or model_tokenizer.split("/")[-1]
```
`return value or ...` 即「用户给了就用用户的，没给就从 tokenizer 推」。这是纯粹的值转换。

**用法二：共享状态。** `validate_api_backend` 查注册表把对应的 `User` 子类存进 `ctx.obj`：

> [validation.py:L257-L270](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L257-L270)
```python
def validate_api_backend(ctx, param, value):
    api_backend = value.lower()
    user_class = API_BACKEND_USER_MAP.get(api_backend)
    if not user_class:
        raise click.BadParameter(f"{value} is not a supported API backend.")
    if ctx.obj is None:
        ctx.obj = {}
    ctx.obj["user_class"] = user_class   # ← 共享给 validate_task 和 benchmark 体
    return api_backend
```

注册表本身定义在文件顶部，键是各 `User` 子类的 `BACKEND_NAME`，`vllm`/`sglang` 是指向 `OpenAIUser` 的别名：

> [validation.py:L25-L38](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L25-L38)

**用法三：拒绝非法值 + 读取共享状态。** `validate_task` 先从 `ctx.obj` 取出上一步存的 `user_class`，再校验任务是否被该后端支持：

> [validation.py:L313-L352](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L313-L352)
```python
def validate_task(ctx, param, value):
    task = value.lower()
    user_class = ctx.obj.get("user_class") if ctx.obj else None
    if not user_class:
        raise click.BadParameter("API backend is not set. ...")
    if not user_class.is_task_supported(task):
        ...  # raise click.BadParameter(...)
    ...
    ctx.obj["user_task"] = getattr(user_class, user_class.supported_tasks[task])  # 继续共享
    return task
```

这是一条经典的「回调链」：`validate_api_backend` 写 `ctx.obj["user_class"]` → `validate_task` 读它、再写 `ctx.obj["user_task"]`。最终 `benchmark` 函数体里 [cli.py:L278-L279](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L278-L279) 直接 `ctx.obj.get("user_class")` 取用，无需重复查表。

#### 4.2.4 代码实践

**实践目标**：用 `click.testing.CliRunner` 亲手触发一次回调报错，观察 `BadParameter` 的表现。

**操作步骤**：

1. 写一段最小脚本（示例代码，非项目原有）：
   ```python
   # 示例代码
   import click
   from click.testing import CliRunner
   from genai_bench.cli.validation import validate_api_backend

   @click.command()
   @click.option("--api-backend", callback=validate_api_backend)
   @click.pass_context
   def demo(ctx, api_backend):
       click.echo(f"selected: {ctx.obj}")

   runner = CliRunner()
   result = runner.invoke(demo, ["--api-backend", "not-a-real-backend"])
   print("exit_code:", result.exit_code)
   print(result.output)
   ```
2. 运行它。

**需要观察的现象**：`exit_code` 为非零（2），输出里包含 "not-a-real-backend is not a supported API backend"。

**预期结果**：证明回调在「命令体执行之前」就已生效，非法后端在解析阶段被拦截，`demo` 函数体根本不会运行。

#### 4.2.5 小练习与答案

**练习**：`validate_api_key`（[validation.py:L273-L310](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L273-L310)）对一个「不需要 API key」的后端却传了 key 时，为什么不抛异常而是 `click.echo(Warning...)` 后 `return None`？

**参考答案**：因为这不是「错误」而是「多余」。OCI/AWS/GCP 等后端用各自的云认证，传统 API key 对它们无效。项目选择「温和提醒 + 丢弃」而非硬报错，避免用户因历史习惯传了无用的 `--api-key` 就跑不起来。同时 `return None` 把这个无用值清掉，防止它污染后续流程。这体现了回调不仅能「拒绝」，还能「修正」。

---

### 4.3 跨参数约束：签名顺序、`is_eager` 与 `ctx.params`

#### 4.3.1 概念说明

很多校验**无法靠单个选项完成**，必须同时看几个选项。比如：

- `--iteration-type` 该用 `num_concurrency` 还是 `batch_size`？取决于 `--task`。
- `--cooldown-ratio` 合法吗？要和 `--warmup-ratio` 加起来看。
- `--upload-results` 能开吗？要先看 `--storage-bucket` 有没有给。

这些都是「跨参数约束」。click 没有内置的「多选项联合校验」语法，genai-bench 用两条规则 + 一个共享钱包来实现：

1. **签名顺序 = 处理顺序**：参数按 `benchmark` 形参从左到右的顺序被处理，所以「排在前面的回调」总能被「排在后面的回调」读到。
2. **`is_eager=True` 抢跑**：标了 eager 的选项会**浮到所有非 eager 选项之前**处理，不管它在签名里的位置。

共享钱包有两个：`ctx.params`（已解析参数字典，渐进填充）和 `ctx.obj`（任意对象袋）。

#### 4.3.2 核心流程

click 实际做**两趟**处理：

```
第 1 趟：按声明顺序处理所有 is_eager=True 的参数
第 2 趟：按声明顺序处理所有非 eager 参数
        └─ 每处理一个，就把它放进 ctx.params
        └─ 其 callback 可读 ctx.params（含本趟已处理的 + 第 1 趟全部）
```

因此一个非 eager 回调能读到：所有 eager 参数 + 所有在它之前声明的非 eager 参数。

#### 4.3.3 源码精读

**例 1：`iteration_type` ↔ `task` 联动（eager 的关键作用）。**
`validate_iteration_params` 需要同时读 `task`、`num_concurrency`、`batch_size` 三者：

> [validation.py:L208-L249](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L208-L249)
```python
def validate_iteration_params(ctx, param, value) -> str:
    task = ctx.params.get("task")
    num_concurrency = ctx.params.get("num_concurrency", [])
    batch_size = ctx.params.get("batch_size", [])
    if task == "text-to-embeddings" or task == "text-to-rerank":
        ...
        value = "batch_size"; num_concurrency = [1]
    else:
        ...
        value = "num_concurrency"; batch_size = [1]
    ctx.params.update({          # ← 回调反过来改写兄弟参数！
        "iteration_type": value,
        "batch_size": batch_size,
        "num_concurrency": num_concurrency,
    })
    return value
```

问题来了：在 `benchmark` 签名里，顺序是 `task, iteration_type, num_concurrency, ..., batch_size`（见 [cli.py:L82-L87](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L82-L87)），即 `iteration_type` 排在 `num_concurrency`/`batch_size` **之前**。按签名顺序，处理 `iteration_type` 时后两者还没进 `ctx.params`，怎么会读得到？

答案就是 `is_eager=True`。看选项定义：

> `--batch-size`：[option_groups.py:L580-L596](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L580-L596)（含 `is_eager=True`）
> `--num-concurrency`：[option_groups.py:L597-L612](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L597-L612)（含 `is_eager=True`）

两者都 eager，所以在第 1 趟就处理完并塞进 `ctx.params`；`--iteration-type` 非 eager，在第 2 趟处理时自然能读到。这是「eager 抢跑」最典型的用法。

这条回调还演示了一个高级技巧：**回调可以反向改写兄弟参数**。对 embeddings/rerank 任务，它强行把 `num_concurrency` 设成 `[1]`、`iteration_type` 设成 `"batch_size"`，相当于「任务类型自动决定迭代维度」。这也正是 u2-l1 提到的「embeddings/rerank 用 `batch_size`，其余用 `num_concurrency`」在 CLI 层的落地。

**例 2：`warmup_ratio` + `cooldown_ratio` 必须 < 1.0。**
`--cooldown-ratio` 的回调做加法校验：

> [validation.py:L422-L432](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L422-L432)
```python
def validate_warmup_cooldown_ratio_options(ctx, param, value):
    warmup_ratio = ctx.params.get("warmup_ratio") or 0.0
    cooldown_ratio = value or 0.0
    if warmup_ratio + cooldown_ratio >= 1.0:
        raise click.BadParameter(
            f"warmup_ratio({warmup_ratio}) + cooldown_ratio({cooldown_ratio}) must be < 1.0.",
            param_hint=["--warmup-ratio", "--cooldown-ratio"],
        )
    return value
```

约束是一个不等式 \(\text{warmup\_ratio} + \text{cooldown\_ratio} < 1.0\)。这里依赖签名顺序：`warmup_ratio` 在 `cooldown_ratio` 之前（[cli.py:L85-L86](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L85-L86)），故读到的是已解析值。注意它用 `param_hint` 把两个相关选项都标出来，让报错更友好。这条约束在 u4-l2 的聚合层也有对应（warmup/cooldown 半开区间过滤）。

**例 3：`--upload-results` 依赖 `--storage-bucket`。**
`--storage-bucket` 也是 eager：

> [option_groups.py:L699-L704](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L699-L704)

这样 `validate_object_storage_options`（挂在 `--upload-results` 上）就能读到它：

> [validation.py:L408-L419](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L408-L419)
```python
def validate_object_storage_options(ctx, param, value):
    if (param.name == "upload_results" and value
            and not ctx.params.get("storage_bucket")):
        raise click.UsageError(
            "You must provide a storage bucket name (--storage-bucket) when uploading results.")
    return value
```

注意这里用的是 `click.UsageError`（整条命令的用法错误），而不是 `BadParameter`——因为「想上传却没给桶」不是某一个选项的错，而是两个选项组合的错。

#### 4.3.4 代码实践

**实践目标**：用一个「依赖顺序」的反例，直观体会「为什么 `--num-concurrency` 必须 eager」。

**操作步骤**：

1. 临时构造一个最小命令（示例代码，非项目原有），把 `--num-concurrency` 的 `is_eager` 去掉，再调用 `validate_iteration_params`：
   ```python
   # 示例代码（仅用于观察，不改项目源码）
   import click
   from click.testing import CliRunner
   from genai_bench.cli.validation import validate_iteration_params

   @click.command()
   @click.option("--task", default="text-to-embeddings")
   @click.option("--num-concurrency", multiple=True, default=(1, 2))  # 故意不 eager
   @click.option("--iteration-type", callback=validate_iteration_params, default="num_concurrency")
   def demo(task, num_concurrency, iteration_type):
       click.echo(f"iteration_type={iteration_type}")

   runner = CliRunner()
   print(runner.invoke(demo, []).output)
   ```
2. 对比：再把 `--num-concurrency` 加回 `is_eager=True`，重跑。

**需要观察的现象**：去掉 eager 时，`validate_iteration_params` 内 `ctx.params.get("num_concurrency", [])` 取到的是空 `[]`（因为还没处理），于是走 `batch_size = batch_size or DEFAULT_BATCH_SIZES` 分支——虽然这里恰好兜住了，但说明顺序依赖是真实存在的；而在真实的 `--batch-size` / `--num-concurrency` 上项目就是靠 eager 保证非空。

**预期结果**：你会理解 `is_eager` 不是装饰，而是「跨参数读取」的正确性保证。真实运行行为以本地为准（「待本地验证」具体打印串）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `validate_api_backend` → `validate_task` 用 `ctx.obj`，而 `validate_iteration_params` 用 `ctx.params`？两者能否互换？

**参考答案**：`ctx.obj` 适合放「解析后新造出来的对象」（如 `user_class`、`user_task` 这些不在选项列表里的东西）；`ctx.params` 只能放「选项本身」。`user_class` 是查表得到的，不是任何选项的值，所以必须放 `ctx.obj`。`iteration_type`/`batch_size`/`num_concurrency` 本身就是选项，所以用 `ctx.params` 并就地改写。两者不可互换：你无法把 `user_class` 塞进 `ctx.params`（它没有对应选项名），也无法把 `num_concurrency` 放进 `ctx.obj` 后还指望 `benchmark` 形参自动收到。

**练习 2**：`--traffic-scenario` 的回调 `validate_traffic_scenario_callback`（[validation.py:L151-L169](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L151-L169)）依赖 `task`。它靠什么保证 `task` 已被处理？

**参考答案**：靠签名顺序。在 `experiment_options` 里 `--task` 不在该组，但 `task` 在 `benchmark` 签名里排在 `traffic_scenario` 之前，且两者都非 eager，所以按声明顺序 `task` 先处理、先入 `ctx.params`，回调即可读到。若有人误把 `--traffic-scenario` 标成 eager，就会读不到 `task` 而抛 "The '--task' option is required but was not provided."。

---

### 4.4 后置整体校验：`validate_prefix_options` 与三种校验风格

#### 4.4.1 概念说明

前两节的回调都是「单选项触发、解析期内执行」。但 `--prefix-len` / `--prefix-ratio` 的约束太复杂：

- 两者互斥；
- 仅对 `text-to-text` 有效；
- 不能与 dataset 模式混用；
- `--prefix-ratio` 要求所有场景都是确定性的 `D(...)`；
- `--prefix-len` 要小于所有场景的最小可能输入 token 数。

这些约束牵涉 6 个参数（`prefix_len, prefix_ratio, task, dataset_path, dataset_config, traffic_scenario`），且逻辑层层嵌套，塞进任何一个单选项的回调都会又长又脆。

于是 genai-bench 采用**第三种校验风格：解析完成后，在命令体里显式调用一个普通函数做整体校验**。它不是回调，签名也不带 `ctx/param`，而是直接接收所有相关参数。

三种校验风格对照：

| 风格 | 触发时机 | 签名 | 报错类型 | 代表 |
| --- | --- | --- | --- | --- |
| 单选项回调 | 解析期，每个选项各自触发 | `callback(ctx, param, value)` | `BadParameter` | `validate_task` |
| 跨参数回调 | 解析期，靠顺序/eager 联动 | `callback(ctx, param, value)` | `BadParameter` / `UsageError` | `validate_iteration_params` |
| **后置整体校验** | **命令体开头，全部解析完成后** | **普通函数，显式传参** | `UsageError` | `validate_prefix_options` |

#### 4.4.2 核心流程

`validate_prefix_options` 的判定是一棵决策树：

```
prefix_len 与 prefix_ratio 都给了?  → 是: 报「互斥」
两者都没给?                        → 是: 直接返回（无需校验）
否则确定 option_name = 前缀 len 或 ratio
  task != "text-to-text"?           → 是: 报「仅支持 text-to-text」
  (有 dataset 且无显式 scenario)?   → 是: 报「需配合 traffic scenario」
  scenario 里含 "dataset"?          → 是: 报「不支持 dataset 模式」
  用的是 prefix_ratio?
    存在非 D(...) 场景?             → 是: 报「ratio 要求全部确定性场景」
  用的是 prefix_len?
    某场景无法算最小输入 token?      → 是: 报「仅支持 D/N/U」
    某场景最小输入 token < prefix_len? → 是: 报「len 须 ≤ 最小输入 token」
```

其中「最小可能输入 token」由辅助函数 `_get_min_input_tokens` 按场景类型估算：

- 确定性 `D(n,m)`：直接取 \(n\)；
- 正态 `N(\mu,\sigma)`：取实用下界 \(\max(1,\ \mu - 3\sigma)\)（约覆盖 99.7% 样本）；
- 均匀 `U(min,max)`：取 `min_input_tokens`（或缺省 1）；
- `E/R/I` 等非文本场景：返回 `None`（无法估算 → 不支持）。

#### 4.4.3 源码精读

主校验函数（注意它不在任何 `callback=` 里，而是被命令体直接调用）：

> [validation.py:L459-L543](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L459-L543)

关键片段：

```python
def validate_prefix_options(prefix_len, prefix_ratio, task,
                            dataset_path, dataset_config, traffic_scenario):
    # 1) 互斥
    if prefix_len is not None and prefix_ratio is not None:
        raise click.UsageError("--prefix-len and --prefix-ratio are mutually exclusive. ...")
    if prefix_len is None and prefix_ratio is None:
        return
    option_name = "--prefix-len" if prefix_len is not None else "--prefix-ratio"
    # 2) 任务兼容
    if task != "text-to-text":
        raise click.UsageError(f"{option_name} is only supported for text-to-text tasks, ...")
    # 3) dataset 模式不兼容
    if (dataset_path or dataset_config) and not traffic_scenario:
        raise click.UsageError(f"{option_name} requires a traffic scenario. ...")
    if traffic_scenario:
        if "dataset" in traffic_scenario:
            raise click.UsageError(f"{option_name} is not supported with dataset mode. ...")
        # 4) ratio 要求全确定性
        if prefix_ratio is not None:
            non_deterministic = [s for s in traffic_scenario if not s.strip().startswith("D(")]
            if non_deterministic:
                raise click.UsageError("--prefix-ratio requires all traffic scenarios to be deterministic. ...")
        # 5) len 要求 <= 最小输入 token
        if prefix_len is not None:
            ...  # 遍历场景，用 _get_min_input_tokens 逐个比较
```

最小输入 token 的估算逻辑：

> [validation.py:L435-L456](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L435-L456)
```python
def _get_min_input_tokens(scenario):
    if hasattr(scenario, "num_input_tokens"):                       # D
        return scenario.num_input_tokens
    if hasattr(scenario, "mean_input_tokens") and hasattr(...):     # N
        return max(1, scenario.mean_input_tokens - 3 * scenario.stddev_input_tokens)
    if hasattr(scenario, "min_input_tokens"):                       # U
        return scenario.min_input_tokens or 1
    return None                                                     # E/R/I 等 → 不支持
```

`scenario` 对象由 `Scenario.from_string(scenario_str)` 解析得到——这正是 u2-l2 讲过的「场景字符串 → Scenario 对象」工厂。这些属性（`num_input_tokens`、`mean_input_tokens/stddev_input_tokens`、`min_input_tokens`）分别来自 `DeterministicDistribution`、`NormalDistribution`、`UniformDistribution` 的构造（见 [scenarios/text.py:L14-L128](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/text.py#L14-L128)）。

**调用点**在 benchmark 命令体里，紧随 tokenizer 加载之后：

> [cli.py:L288-L296](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L288-L296)
```python
# Validate prefix options after all CLI parameters are parsed
validate_prefix_options(
    prefix_len=prefix_len,
    prefix_ratio=prefix_ratio,
    task=task,
    dataset_path=dataset_path,
    dataset_config=dataset_config,
    traffic_scenario=traffic_scenario,
)
```

注释 "after all CLI parameters are parsed" 一语道破这种风格的本质：**它故意推迟到所有解析结束后才跑**，因此能毫无顺序顾虑地拿到全部 6 个参数。代价是报错时机比回调晚一点（但仍在发请求之前，不影响安全）。

#### 4.4.4 代码实践

**实践目标**：依照本讲实践任务要求，构造一组会触发 `--prefix-len` / `--prefix-ratio` 报错的参数组合，复现并解释每条错误。依据是项目自带的单元测试 [tests/cli/test_validation.py:L565-L687](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/cli/test_validation.py#L565-L687)。

**操作步骤**：

1. 写一个最小复现脚本（示例代码），逐一调用 `validate_prefix_options` 并捕获异常：
   ```python
   # 示例代码
   import click
   from genai_bench.cli.validation import validate_prefix_options

   cases = [
       # (说明, kwargs)
       ("互斥：len 和 ratio 同时给",
        dict(prefix_len=50, prefix_ratio=0.5, task="text-to-text",
             dataset_path="d", dataset_config=None, traffic_scenario=["D(100,50)"])),
       ("dataset 出现在 scenario 列表",
        dict(prefix_len=50, prefix_ratio=None, task="text-to-text",
             dataset_path="d", dataset_config=None, traffic_scenario=["D(100,50)", "dataset"])),
       ("ratio 遇到非确定性场景 N(...)",
        dict(prefix_len=None, prefix_ratio=0.5, task="text-to-text",
             dataset_path="d", dataset_config=None,
             traffic_scenario=["D(100,50)", "N(480,240)/(300,150)"])),
       ("len 超过确定性场景的最小输入 token",
        dict(prefix_len=150, prefix_ratio=None, task="text-to-text",
             dataset_path="d", dataset_config=None, traffic_scenario=["D(100,50)", "D(200,100)"])),
       ("len 超过均匀场景的最小输入 token",
        dict(prefix_len=150, prefix_ratio=None, task="text-to-text",
             dataset_path="d", dataset_config=None, traffic_scenario=["U(100,200)/(50,100)"])),
       ("非 text-to-text 任务",
        dict(prefix_len=50, prefix_ratio=None, task="text-to-embeddings",
             dataset_path=None, dataset_config=None, traffic_scenario=["E(1024)"])),
   ]
   for desc, kw in cases:
       try:
           validate_prefix_options(**kw)
           print(f"[未报错] {desc}")
       except click.UsageError as e:
           print(f"[报错] {desc}\n       → {e.message}")
   ```
2. 运行脚本。

**需要观察的现象与每条错误的触发条件**：

| 用例 | 触发条件 | 对应报错关键词 |
| --- | --- | --- |
| 互斥 | `prefix_len` 与 `prefix_ratio` 同时非 `None` | "mutually exclusive" |
| dataset 在列表 | `traffic_scenario` 含字符串 `"dataset"` | "not supported with dataset mode" |
| ratio + 非确定性 | `prefix_ratio` 非 `None` 且存在不以 `D(` 开头的场景 | "all traffic scenarios to be deterministic" |
| len 超限（D） | `prefix_len > num_input_tokens`（D 场景的最小输入即其输入值） | "must be <= minimum input tokens" |
| len 超限（U） | `prefix_len > min_input_tokens`（U(100,200) 的 min=100 < 150） | "must be <= minimum input tokens"，且提示 `min_input_tokens=100` |
| 非 text-to-text | `task != "text-to-text"` | "only supported for text-to-text tasks" |

**预期结果**：前 5 个用例与 [test_validation.py:L565-L687](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/cli/test_validation.py#L565-L687) 的断言一致（该测试用 `pytest.raises(click.UsageError)` 配合 `assert "..." in str(exc.value)` 逐一覆盖）；第 6 个用例对应 [validation.py:L481-L484](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L481-L484) 的任务兼容分支。若想验证「合法组合不报错」，可加上 `prefix_len=50, traffic_scenario=["D(100,50)","D(200,100)"]`，应打印 `[未报错]`，对应测试里 L679-L687 的正向用例。

> 提示：这套校验与 u2-l4 讲的 prefix 缓存机制是「校验端」与「使用端」的关系——本讲只管「参数合不合法」，缓存如何生成、如何跨 worker 复用，已在 u2-l4 详述。

#### 4.4.5 小练习与答案

**练习 1**：`N(500,100)/(300,50)` 配 `--prefix-len 50` 为什么能通过？（见测试 L631-L641）

**参考答案**：正态场景的最小可能输入 token = \(\max(1, 500 - 3\times100) = \max(1,200) = 200\)，而 `prefix_len=50 \le 200`，故通过。这体现了 `--prefix-len`（与 `--prefix-ratio` 不同）**允许非确定性场景**，只要下界够大即可。

**练习 2**：为什么 `--prefix-ratio` 要求「全部确定性场景」，而 `--prefix-len` 不要求？

**参考答案**：`--prefix-ratio` 是「按每次请求的实际输入长度乘以比例」动态生成前缀（u2-l4），若输入长度本身随机（N/U），前缀长度就会逐请求漂移，无法稳定命中 KV-cache；故要求 `D(...)` 确定性场景。`--prefix-len` 是「固定 token 数的全局前缀」，与单次请求输入长度无关，所以能兼容任意场景，只需保证该固定值不超过任何场景的最小输入即可。

**练习 3**：`validate_prefix_options` 为什么全部用 `click.UsageError` 而不用 `click.BadParameter`？

**参考答案**：因为这些矛盾大多不是「某一个选项的值本身非法」（`--prefix-len 50` 单看完全合法），而是「几个选项组合起来才冲突」。`UsageError` 表达「整条命令用法不对」，语义更准确；而且它是后置校验、已脱离某个具体 `param` 上下文，没有合适的单选项可指。对比 `validate_warmup_cooldown_ratio_options` 虽也是跨参数，但仍发生在解析期内、能拿到 `param`，所以用 `BadParameter` + `param_hint` 更贴切。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「读参 → 排错 → 解释」的小任务。

**任务**：假设有用户抱怨下面这条命令「怎么改都报错」，请你当一次 CLI 侦探：

```bash
genai-bench benchmark \
  --api-backend sglang --api-base http://x --api-model-name M \
  --model-tokenizer meta-llama/X --task text-to-embeddings \
  --prefix-ratio 0.5 --traffic-scenario "N(480,240)/(300,150)" \
  --max-time-per-run 1 --max-requests-per-run 10
```

**要求**：

1. **指出它会依次撞上哪些校验**。提示：先想 `validate_iteration_params` 会把 `iteration_type` 改成什么、把 `num_concurrency`/`batch_size` 改成什么（4.3 例 1）；再想 `validate_prefix_options` 会因哪两条先后报错（4.4）。
2. **给出两套修复方案**：
   - 方案 A：保留 `--prefix-ratio`，调整 `--traffic-scenario` 使其合法。
   - 方案 B：改用 `--prefix-len`，并说明它对场景的约束与 ratio 有何不同。
3. **画一张时序图**，标注从 click 解析开始，到 `benchmark` 函数体里 `validate_prefix_options(...)` 被调用为止，各回调（`validate_api_backend` → `validate_task` → `validate_iteration_params` → `validate_prefix_options`）的执行先后与各自读/写的 `ctx.obj` 或 `ctx.params` 键。

**参考思路**：

- 这条命令 `task=text-to-embeddings`，故 `validate_iteration_params` 会强制 `iteration_type="batch_size"`、`num_concurrency=[1]`、`batch_size=DEFAULT_BATCH_SIZES`，并打印一条 "Note: Using batch_size iteration ..."。
- 紧接着 `validate_prefix_options` 会先因「`--prefix-ratio` 要求全部确定性场景」对 `N(...)` 报错；即便改成全 `D(...)`，还要确认 `task` 必须是 `text-to-text`——而这里是 `text-to-embeddings`，所以**根因**其实是「prefix 选项根本不支持 embeddings 任务」，应优先改任务或去掉 prefix 选项。
- 方案 A：把 `--task` 改回 `text-to-text`，并把场景改成 `--traffic-scenario "D(100,50)"`，`--prefix-ratio 0.5` 即可生效。
- 方案 B：同样先改回 `text-to-text`，用 `--prefix-len 50 --traffic-scenario "D(100,50)"`；它与 ratio 的差别在于允许 `N/U` 场景（只要最小输入 ≥50），但不接受 `E/R/I` 等非文本场景。

完成本任务后，你应能对任意一条报错的 benchmark 命令，快速判断它是「单选项错」「跨参数错」还是「后置整体错」，并定位到对应的校验函数。

## 6. 本讲小结

- `benchmark` 的约 70 个选项被拆成 10 个「选项组装饰器」（如 `api_options`、`sampling_options`），每个就是一摞 `click.option` 的打包，靠「倒序」规则决定最终签名顺序。
- 校验回调签名固定为 `callback(ctx, param, value)`，可同时承担「转换值 / 共享状态 / 拒绝非法值」三种职责，代表是 `set_model_from_tokenizer`、`validate_api_backend`、`validate_task`。
- 跨参数约束靠两条规则实现：**签名顺序决定处理顺序**、**`is_eager=True` 抢跑到所有非 eager 之前**；共享钱包是 `ctx.params`（渐进填充的已解析参数）与 `ctx.obj`（任意对象袋）。
- `validate_iteration_params` 是「回调反向改写兄弟参数」的典型：按 `task` 把 `iteration_type`/`num_concurrency`/`batch_size` 强行归一，这是「任务类型决定迭代维度」在 CLI 层的落地。
- `validate_prefix_options` 是第三种校验风格——**解析完成后在命令体里显式调用的整体校验**，用 `click.UsageError` 表达跨多选项的矛盾，与单选项回调的 `BadParameter` 形成对照。

## 7. 下一步学习建议

- 想看「这些被校验、被归一后的参数如何驱动主流程」，回到 u8-l1 的 benchmark 七段流水线，重点对照本讲的 `ctx.obj["user_class"]`、`iteration_type`、`traffic_scenario` 是如何流入双层循环的。
- 想亲手扩展校验或选项，阅读 u8-l3「扩展指南」，它会讲如何新增一个选项组、新增一个校验回调，并保持与现有顺序/eager 规则一致。
- 若你对 prefix 选项通过校验后的实际行为（缓存生成、跨 worker 复用、重置时机）感兴趣，复习 u2-l4「采样器与请求构造」的 prefix 缓存机制一节。
- 直接阅读源码时，建议按本讲「三种校验风格对照表」分类梳理 [validation.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py) 里的每一个函数，会比逐行阅读更快建立全局观。
