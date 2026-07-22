# 启动流程与 ServerArgs 只读配置模型

## 1. 本讲目标

本讲解决一个问题:**SGLang 启动时,配置是怎么进入运行时的,以及为什么运行期不能随便改它?**

学完本讲你应该能够:

1. 跟踪从命令行 `sglang serve` 一路到 `http_server.launch_server` 的完整启动调用链。
2. 理解 `ServerArgs` 这个数据类的新模型:`__post_init__` 之后**只读**、每个字段用 `NS("namespace")` 标注它属于哪个命名空间。
3. 掌握配置的**唯一合法改写入口** `override()`,以及它背后的 `_in_override` 守卫为什么能挡住裸赋值。
4. 看懂声明式覆盖的装配闸门 `materialize_declarations`:模型代码「声明」要改什么,而不是直接改。
5. 用本轮新增的 **sidecar(本地 gRPC 旁路进程)** 功能把上述概念串起来:`sidecar`/`sidecar_args` 两个字段如何归入 `serving` 命名空间、在 `__post_init__` 被校验、最终在 `http_server` 的 lifespan 里被启动与回收。

本讲是理解整个运行时配置体系的「地基」。下一讲(u2-l5)会讲 `RuntimeContext` 如何把这里的命名空间发布成进程全局可读的配置袋。

---

## 2. 前置知识

阅读本讲前,你需要知道(来自 u1 / u2-l1):

- **配置对象 `ServerArgs`**:一个装满启动参数的数据类(`@dataclasses.dataclass`),从命令行参数构造而来,是整个进程的「配置源头」。
- **`__post_init__`**:Python dataclass 在 `__init__` 之后自动调用的钩子,常用来做参数校验和默认值填充。SGLang 在这里做大量配置「解析」工作。
- **`typing.Annotated`**:Python 标准库的类型标注工具,允许你在类型旁边附加「元数据」。本讲中你会看到 `A` 就是 `Annotated` 的别名,元数据里塞进了 CLI 帮助文本、`Arg(...)` 和 `NS(...)`。
- **多进程架构**:SGLang 由 TokenizerManager、Scheduler、DetokenizerManager 等进程组成。每个进程都会各自构造一份 `ServerArgs`,所以「配置怎么来、能不能改」是所有进程共同的问题。

如果你还不熟悉进程拓扑,请先读 u2-l1。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/sglang/cli/serve.py` | `sglang serve` 子命令的入口,做模型类型分流,最终调用 `run_server`。 |
| `python/sglang/launch_server.py` | `run_server(server_args)`:按 gRPC/Ray/encoder/默认 HTTP 四种模式分发。 |
| `python/sglang/srt/server_args.py` | `ServerArgs` 数据类本体、`__post_init__` 解析编排、`override()`、`__setattr__` 守卫、`prepare_server_args()`,以及本轮新增的 `sidecar`/`sidecar_args` 字段与校验。 |
| `python/sglang/srt/arg_groups/arg_utils.py` | `NS` 命名空间标记、`Arg` CLI 元数据、`resolvable_fields`/`namespace_of` 等反射工具。 |
| `python/sglang/srt/arg_groups/overrides.py` | 声明式覆盖注册表:`MODEL_OVERRIDES`、`@register_model_override`、`@register_post_process`、`materialize_declarations`、`validate_declarations`。 |
| `python/sglang/srt/entrypoints/http_server.py` | 默认模式的 `launch_server()` 与 FastAPI 的 `lifespan`:拉起三个子进程、启动原生 gRPC server,并随主服务启动/停止 sidecar。 |
| `python/sglang/srt/entrypoints/sidecar.py` | 本轮新增:本地托管的 sidecar 进程启动器(`start_sidecar`、`Sidecar`、`_run_sidecar`)。 |

---

## 4. 核心概念与源码讲解

本讲按五个最小模块展开,顺序是:**配置对象长什么样(4.1)→ 配置在启动时怎么被解析装配(4.2)→ 运行期怎么合法地改(4.3)→ 这一切发生在启动链路的哪一段(4.4)→ 用一个真实新增功能 sidecar 把前四步串起来(4.5)**。

### 4.1 ServerArgs 数据类与 NS 命名空间标注

#### 4.1.1 概念说明

`ServerArgs` 是 SGLang 的「配置源头」。你可以把它想象成一张巨大的表格:每一行是一个配置项(`model_path`、`tp_size`、`chunked_prefill_size`……),所有进程都从这张表读取自己需要的那部分。

但「配置项」并不只是「名字 + 值」。SGLang 给每个字段额外挂了三种元数据:

1. **CLI 元数据**(`Arg(...)`)或裸帮助字符串:决定这个字段在命令行上叫什么(`tp_size` → `--tp-size`)、有哪些可选值。
2. **命名空间标记 `NS("...")`**:声明这个字段属于哪个逻辑命名空间(如 `model`、`parallel`、`schedule`、`serving`)。这是本讲的重点。
3. **是否可解析 `Arg(resolvable=True)`**:声明这个字段是否允许被「声明式覆盖」改写(见 4.2)。

为什么要引入命名空间?因为 `ServerArgs` 有几百个字段,直接平铺会让下游代码「什么都得从一整个 god object 里捞」。命名空间的本质是:**给字段打上分类标签,这样下游(下一讲的 `RuntimeContext`)就能按子系统把字段聚合成「配置袋(config bags)」**,调度器读 `schedule` 袋、并行层读 `parallel` 袋,各取所需。

#### 4.1.2 核心流程

字段定义的形态(伪代码):

```
字段名: Annotated[类型, CLI元数据, NS("命名空间")] = 默认值
```

- `A` 就是 `typing.Annotated` 的别名(见 [arg_utils.py:58](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/arg_utils.py#L58))。
- 第三段 `NS("...")` 是命名空间标记,它和 `Arg(...)` 是**两个并列的元数据元素**——刻意分开,是为了给现有几百个字段「补」命名空间时,只需在末尾追加一个 `NS(...)`,而不必重写每个 `Arg(...)` 调用。
- 反射函数 `namespace_of(cls)` 扫描所有字段,把带 `NS` 标记的字段汇总成 `{字段名: 命名空间路径}` 的字典,供下游建树。
- 反射函数 `resolvable_fields(cls)` 则挑出 `Arg(resolvable=True)` 的字段,作为「可被声明式覆盖」的白名单。

字段 → 命名空间的映射关系,可以直观写成:

\[
\text{namespace\_of}(\text{ServerArgs}) = \{\, f \mapsto \text{NS.path} \;\mid\; f \text{ 的标注里含 } \text{NS} \,\}
\]

#### 4.1.3 源码精读

先看 `ServerArgs` 类的整体形态和文档对标注风格的约定:类文档的 "Adding new arguments" 段明确要求新字段必须用 `A[T, ...]` 标注风格。

[python/sglang/srt/server_args.py:412-452](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L412-L452) —— `@dataclasses.dataclass` 装饰的 `ServerArgs`,文档第 2 条([server_args.py:423-451](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L423-L451))规定一切字段都用 `A[T, ...]`,并给出裸字符串与 `Arg(...)` 两种写法的示例。

看第一个字段 `model_path`,体会「类型 + Arg + NS」三段式:

[python/sglang/srt/server_args.py:457-464](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L457-L464) —— `model_path: A[str, Arg(help=..., aliases=["--model"]), NS("model")]`,归入 `model` 命名空间。

本讲实践任务涉及的另外四个字段,命名空间标注如下(请对照源码确认):

- [server_args.py:722-726](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L722-L726) —— `mem_fraction_static` 归入 `NS("schedule")`(注意:它落在 "Memory and scheduling" 段落,但命名空间是 `schedule`,不是 `memory`)。
- [server_args.py:749-753](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L749-L753) —— `chunked_prefill_size` 归入 `NS("schedule")`。
- [server_args.py:953-960](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L953-L960) —— `tp_size` 归入 `NS("parallel")`。
- [server_args.py:985-992](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L985-L992) —— `dp_size` 归入 `NS("parallel")`。

本轮新增的 `sidecar`/`sidecar_args` 两个字段也用同样的三段式,归入 `serving` 命名空间(4.5 详述):

- [server_args.py:1185-1202](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L1185-L1202) —— `sidecar` 用裸字符串帮助文本,`sidecar_args` 用 `Arg(type_parser=json_list_type)`,两者末尾都带 `NS("serving")`。

再看 `NS` 本体和两个反射函数:

[python/sglang/srt/arg_groups/arg_utils.py:85-98](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/arg_utils.py#L85-L98) —— `NS` 是一个 `frozen` dataclass,只有一个 `path: str` 字段。注释解释了它为何与 `Arg` 分离(第 93-95 行:为了让现有约 400 个字段「追加一个元素」就能获得命名空间,而不必重写每个 `Arg(...)`)。

[arg_utils.py:101-119](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/arg_utils.py#L101-L119) —— `namespace_of(cls)`:用 `get_type_hints(..., include_extras=True)` 解析出每个字段的 `Annotated` 元数据,挑出其中的 `NS` 实例,汇总成字典。`@functools.lru_cache` 保证只算一次。

[arg_utils.py:122-137](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/arg_utils.py#L122-L137) —— `resolvable_fields(cls)`:挑出 `Arg.resolvable == True` 的字段名集合,作为声明式覆盖的白名单。

[arg_utils.py:61-82](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/arg_utils.py#L61-L82) —— `Arg` dataclass,`resolvable: bool = False`(第 82 行)说明:默认字段不可被声明式覆盖,只有显式标记的才进白名单。

> 术语提示:`Annotated[T, m1, m2]` 表示「类型是 T,附带元数据 m1、m2」。`get_origin` 会返回 `Annotated`,`get_args` 返回 `(T, m1, m2)`。

#### 4.1.4 代码实践

**实践目标**:确认五个字段的命名空间归属,并理解命名空间与字段所在「代码段落」并不一定一致。

**操作步骤**:

1. 在 `server_args.py` 中分别定位 `model_path`、`tp_size`、`dp_size`、`chunked_prefill_size`、`mem_fraction_static` 五个字段的定义。
2. 读出每个字段标注里的 `NS("...")`。
3. 对照下表(本讲已为你填好,请到源码里逐条核对):

| 字段 | 所在代码段落(注释 banner) | NS 命名空间 |
| --- | --- | --- |
| `model_path` | Model and tokenizer | `model` |
| `tp_size` | Parallelism | `parallel` |
| `dp_size` | Parallelism | `parallel` |
| `chunked_prefill_size` | Memory and scheduling | `schedule` |
| `mem_fraction_static` | Memory and scheduling | `schedule` |
| `sidecar` / `sidecar_args` | Server / serving | `serving` |

**需要观察的现象**:`mem_fraction_static` 的注释段落是 "Memory and scheduling",但它的命名空间是 `schedule`,而**不是** `memory`。这说明:**命名空间是逻辑分类,不等于字段在文件里的物理位置**。同理 `sidecar`/`sidecar_args` 与同段的 `grpc_port`、`host`、`port` 一起,都属于 `serving`。

**预期结果**:你能说出「命名空间由 `NS(...)` 显式声明决定,与字段在源码中写在哪个段落无关」。如果你之后要给新字段加命名空间,必须显式追加 `NS(...)`,不能依赖它写在哪个注释块下。

**待本地验证**:你可以运行下面这条「源码阅读型」命令,看看 `namespace_of` 实际会产出什么(不需要起服务):

```bash
python -c "from sglang.srt.arg_groups.arg_utils import namespace_of; from sglang.srt.server_args import ServerArgs; m = namespace_of(ServerArgs); print({k: m[k] for k in ['model_path','tp_size','dp_size','chunked_prefill_size','mem_fraction_static','sidecar','sidecar_args']})"
```

(若该环境的依赖未装齐导致导入失败,则改为人工对照源码表填写,标注「待本地验证」。)

#### 4.1.5 小练习与答案

**练习 1**:`NS` 为什么设计成一个独立的标记类,而不是塞进 `Arg` 的一个字段?

**参考答案**:为了让几百个已经写成「裸字符串帮助文本」或 `Arg(...)` 的字段,**只需在标注末尾追加一个 `NS(...)` 元素**就能获得命名空间,而不必把每个 `Arg(...)` 调用都重写一遍。`namespace_of` 直接从 `Annotated` 的元数据里挑 `NS` 实例即可。

**练习 2**:`resolvable_fields(ServerArgs)` 返回的字段,和 `namespace_of(ServerArgs)` 返回的字段,是同一批吗?

**参考答案**:不一定。前者由 `Arg(resolvable=True)` 决定(谁能被声明式覆盖改写),后者由有没有 `NS` 标记决定(属于哪个命名空间)。一个字段可以属于某个命名空间但不可解析,也可以可解析但尚未标命名空间。

---

### 4.2 materialize_declarations:声明式覆盖的装配闸门

#### 4.2.1 概念说明

`ServerArgs` 在 `__post_init__` 里要做大量「解析」:根据模型架构自动选注意力后端、根据硬件选 MoE runner、补默认 `page_size` 等等。这些调整**不能由模型代码直接写字段**——因为那样会把「谁改了什么」埋得无迹可寻,而且解析中途各 handler 之间会互相踩踏。

SGLang 的解法是**声明式覆盖(declarative overrides)**:

- 模型代码**声明**「我想把这个字段改成这个值」,返回一个 `dict`,但**不实际写**字段。
- 所有声明被收集到一个「stash」(`_resolved_overrides`)。
- 直到 `__post_init__` 的最后一步,`materialize_declarations` 才按**闸门顺序(last writer wins,后写者胜)**把整个 stash 一次性应用到字段上。

这样做的好处:解析过程中任何 handler 读字段时,看到的是「原始值 + 已声明覆盖的叠加视图」(`ResolvedView`),而不是被中途改坏的状态;最终结果可审计(每条声明都带 `source` 来源)。

#### 4.2.2 核心流程

声明有三个来源(见 [overrides.py:14-28](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L14-L28) 的模块文档):

1. **常量覆盖 `MODEL_OVERRIDES`**:纯常量情况,`架构 -> {字段: 值}`。如某些模型强制 `dtype="bfloat16"`。
2. **派生覆盖 `@register_model_override(arch)`**:一个可调用对象 `fn(server_args, hf_config) -> dict`,把原来的条件逻辑「忠实搬运」过来,返回声明,绝不写 `server_args`。
3. **后处理 pass `@register_post_process`**:`fn(view) -> dict`,在「规范化阶段」按固定顺序跑,做跨架构的默认值归一化(如根据后端约束修正 `page_size`)。

装配流程(伪代码):

```
__post_init__:
    self._resolved_overrides = []          # 1. 建空 stash
    ... 各 handler 跑(期间用 ResolvedView 读,声明进 stash)...
    materialize_declarations(self)         # 2. 末尾一次性应用
```

「闸门顺序、后写者胜」可以形式化为:对 stash 里按顺序 \( (s_1, d_1), (s_2, d_2), \dots, (s_n, d_n) \),最终某字段 \( f \) 的取值为

\[
\text{value}(f) = d_k[f], \quad k = \max\{\, i \mid f \in d_i \,\}
\]

即最后一个声明该字段的来源胜出。

#### 4.2.3 源码精读

先看 `__post_init__` 的编排风格——它是一个**有序 dispatcher**,每步都是一个 `self._handle_*` 调用:

[python/sglang/srt/server_args.py:3307-3314](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L3307-L3314) —— 注意第 3311 行**在任何短路之前**就建好 `self._resolved_overrides = []`,这样即便走 dummy/none 模型的早退路径(第 3313-3314 行),后续代码也能安全引用它。(`__post_init__` 本体定义在同文件第 3283 行。)

`_handle_model_specific_adjustments` 负责收集模型架构级声明(这是声明的核心入口之一):

[server_args.py:4742-4751](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L4742-L4751) —— 调用 `collect_model_override_declarations(model_arch, self, hf_config)` 把声明装进 `self._resolved_overrides`,并立刻 `validate_declarations` 做白名单校验。

收集函数本身,定义了「常量先、派生按注册序、谓词匹配最后」的应用顺序:

[python/sglang/srt/arg_groups/overrides.py:277-300](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L277-L300) —— `collect_model_override_declarations`。

来看真正「落笔」的闸门函数:

[python/sglang/srt/arg_groups/overrides.py:216-225](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L216-L225) —— `materialize_declarations`:遍历 stash,对每条 `(source, declared)` 用 `setattr` 应用(last writer wins),最后把 `_declarations_materialized` 置为 `True`。**正是这个标志位,在 4.3 中触发只读守卫。**

它在 `__post_init__` 最末尾被调用:

[server_args.py:3486-3488](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L3486-L3488) —— `from ... import materialize_declarations; materialize_declarations(self)`。

配套的还有「中途只读视图」和「白名单校验」:

- [overrides.py:120-143](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L120-L143) —— `ResolvedView`:只读视图,`__setattr__` 直接 raise,逼 pass 只能「返回声明」。
- [overrides.py:173-202](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L173-L202) —— `run_post_process_pass`:在遗留 slot 上跑一个 pass,把它的声明追加进 stash。
- [overrides.py:2203-2223](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L2203-L2223) —— `validate_declarations`:声明时立即做白名单检查,字段不在 `resolvable_fields` 里就报错,做到 fail-fast。

常量与注册装饰器示例:

- [overrides.py:64-69](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L64-L69) —— `MODEL_OVERRIDES` 常量表。
- [overrides.py:80-94](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L80-L94) —— `register_model_override(architecture)` 装饰器,约定被装饰函数返回 `dict` 且不写 `server_args`。

> 一个真实、鲜活的派生覆盖示例(`@register_post_process` 的同类机制):一个典型的派生覆盖会读 `server_args` 后返回声明而非写字段。例如 [overrides.py:448-452](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L448-L452) 的 `_mimo_v2_overrides`,当 `speculative_algorithm == "EAGLE"` 时返回 `{"enable_multi_layer_eagle": True}`,而不是 `server_args.enable_multi_layer_eagle = True`。本轮也在**同一套声明机制**里为新加入的 `hpc_ops` 注意力后端补了 `page_size=64` 的约束(见 [overrides.py:1740-1747](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L1740-L1747),属于 `_mla_backend_page_constraints` 这个 post-process pass)——这正是「声明而不写、统一闸门应用」在现实里持续被使用的证据。

#### 4.2.4 代码实践

**实践目标**:用源码阅读的方式,确认「声明」与「应用」是分离的两步,且应用顺序是 last writer wins。

**操作步骤**:

1. 打开 [overrides.py:448-452](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L448-L452)(`_mimo_v2_overrides`),确认它 `return {...}` 而不是写 `server_args`。
2. 打开 [overrides.py:216-225](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L216-L225)(`materialize_declarations`),确认它是在 stash 收集完毕后才一次性 `setattr`。
3. 在 `server_args.py` 的 `__post_init__`(L3283 起)里,数一下从开头到 L3488 `materialize_declarations(self)` 之间有多少个 `self._handle_*`,体会「解析」的规模。

**需要观察的现象**:`_mimo_v2_overrides` 内部对 `server_args` 只读、返回 dict;`materialize_declarations` 才真正写字段。

**预期结果**:你能用一句话说清——「声明在解析过程中累积,应用在解析末尾一次性发生」,因此中途任何 handler 看到的字段值要么是原始值、要么是声明叠加后的视图,不会被某个 handler 的副作用污染。

**待本地验证**:若想动态观察 stash 内容,可在 `materialize_declarations` 前临时加一行日志(仅本地实验,勿提交)打印 `self._resolved_overrides`,启动一个真实模型查看每个 source 声明了什么。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `ResolvedView` 要把 `__setattr__` 直接 raise?

**参考答案**:为了在解析过程中强制 pass「返回声明」而非「写字段」。如果允许中途写,会破坏「声明与应用分离」的不变量,导致 last-writer-wins 的可预测顺序失效,也让可审计性丢失。

**练习 2**:`validate_declarations` 在什么时候报错?为什么要在「声明时」而不是「应用时」才报?

**参考答案**:它在每次声明进 stash 时(以及 `override()` 时)立即做白名单检查。声明时 fail-fast 能让一个注册表拼写错误或「声明了不可解析字段」在它的 slot 上当场暴露,而不是等到 publish 时才定位困难。

---

### 4.3 override() 与 _in_override:唯一的运行期改写入口

#### 4.3.1 概念说明

`__post_init__` 跑完、`materialize_declarations` 落笔之后,`ServerArgs` 就进入「已解析、只读」状态。它代表**进程启动那一刻的配置快照**。

但有些值在运行期才会确定,或需要被控制面重新配置(比如权重加载后才知道的真实 `dtype`、部署接线时设置的参数)。这些调整不能直接 `server_args.x = y`,因为:

- 直接赋值会绕过一切审计,「谁在什么时候改了什么」无从追踪。
- 多进程下各份 `ServerArgs` 会不一致。
- 下游命名空间袋(下一讲的 `RuntimeContext`)看不到这次改动。

所以 SGLang 规定:**运行期改配置的唯一合法入口是 `override(source, **fields)`**。它做三件事:

1. 把可解析字段(`resolvable_fields` 白名单内)记进 `_resolved_overrides` stash(带 `source` 来源),保证重新发布时能解析出同样的值;
2. 把非白名单字段记进 `_runtime_mutations` 日志,留痕;
3. 在 `_in_override=True` 的保护下真正写字段。

而 `ServerArgs.__setattr__` 被重写成一个**守卫**:只要配置已 materialize(`_declarations_materialized=True`)、且当前不在 `override()` 内部(`_in_override=False`),任何对非下划线字段的裸赋值都会直接 `raise AttributeError`。

> 注意:运行期真正想让「命名空间读者」(如 `get_schedule()`)看到改动,标准做法是走 `get_context().override(...)`(下一讲 u2-l5 详述),它会把值写进配置袋。本讲的 `server_args.override()` 是 `ServerArgs` 层面的统一改写原语,`get_context().override` 在其之上。

#### 4.3.2 核心流程

只读守卫的判定逻辑(伪代码):

```
__setattr__(name, value):
    if (非下划线字段) and (已 materialize) and (不在 override 内):
        raise AttributeError("server_args 只读,请用 override")
    正常赋值
```

`override()` 的执行(伪代码):

```
override(source, **fields):
    白名单内的 -> 追加进 _resolved_overrides (带 source)
    白名单外的 -> 追加进 _runtime_mutations (留痕)
    _in_override = True
    try:
        逐个 setattr(fields)        # 此时守卫放行,因为 _in_override=True
    finally:
        _in_override = False
```

`_in_override` 是一个「临时令牌」:`override()` 用 `object.__setattr__` 直接绕过守卫把它置 True,写字段时守卫看到令牌就放行,写完再置回 False。这样守卫既能挡住外部裸赋值,又不会挡住 `override()` 自己。

#### 4.3.3 源码精读

先看守卫本体:

[python/sglang/srt/server_args.py:7915-7932](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L7915-L7932) —— `__setattr__`:三个条件(第 7922-7925 行)同时成立才 raise:① `name` 非下划线开头;② `_declarations_materialized` 为真;③ `_in_override` 为假。错误信息明确指向「use get_context().override(...)」。注释(第 7920-7921 行)说明这曾是可由环境变量 `SGLANG_STRICT_CONFIG_MUTATION` 关闭的可选项,现已**无条件强制**。

再看唯一合法入口:

[python/sglang/srt/server_args.py:7881-7913](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L7881-L7913) —— `override(self, source, **fields)`:

- L7893-7895:取白名单 `resolvable_fields`,把传入字段分成「可解析 declared」和「其余 rest」两组。
- L7896-7901:可解析字段进 `_resolved_overrides`(带 `source`),保证可重放。
- L7902-7907:其余字段进 `_runtime_mutations` 留痕(不影响重放)。
- L7908-7913:`object.__setattr__` 把 `_in_override` 置 True,然后逐个 `setattr` 字段——此时守卫因令牌放行;`finally` 里置回 False。

而 `materialize_declarations` 内部写字段时之所以不被守卫挡,是因为它在置 `_declarations_materialized=True` **之前**就已经写完了字段(见 4.2.3 的 L216-225:先循环 `setattr`,最后才 `server_args._declarations_materialized = True`)。此外,`overrides.py` 里 `_apply_fields` 这种「在 materialize 之后、由解析管线代写」的场景,也会先用 `object.__setattr__` 把 `_in_override` 置 True 再写(见 [overrides.py:205-213](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py#L205-L213))。

#### 4.3.4 代码实践

**实践目标**:验证「运行期不能直接 `server_args.x = y`,必须走 `override()`」,并理解为何如此设计。

**操作步骤**(纯源码阅读,无需启动服务):

1. 读 [server_args.py:7915-7932](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L7915-L7932),找出触发 `AttributeError` 的三个条件。
2. 读 [server_args.py:7881-7913](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L7881-L7913),确认 `override()` 是如何用 `_in_override` 令牌绕过守卫的。
3. 回答:为什么不能允许运行期直接写 `server_args.chunked_prefill_size = 4096`?

**需要观察的现象**:守卫只对「非下划线 + 已 materialize + 非 override 上下文」三者同时成立时才 raise;`_xxx` 这类内部字段不受限。

**预期结果**(用一段话描述):因为直接赋值会绕过审计、破坏多进程间配置一致性、且让下游命名空间袋看不到改动;`override()` 把改动记录进 stash(可重放)和 mutations 日志(可追溯),并在受控上下文内写字段,既保证安全又保留 provenance。若想让命名空间读者真正看到新值,应进一步走 `get_context().override(...)`(下一讲)。

**待本地验证**(可选,需装齐依赖):写一段最小脚本构造一个真实 `ServerArgs`(走 `prepare_server_args`),然后尝试 `server_args.tp_size = 4`,预期捕获到 `AttributeError`;再用 `server_args.override("demo", tp_size=4)` 验证能成功。注意:这会真实触发 `__post_init__` 全流程,可能需要 GPU 环境,否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**:`_in_override` 为什么用 `object.__setattr__` 直接设置,而不是 `self._in_override = True`?

**参考答案**:因为 `self.x = y` 会触发重写后的 `ServerArgs.__setattr__`。虽然 `_in_override` 是下划线字段、会被守卫放行,但用 `object.__setattr__` 能彻底绕开自定义 `__setattr__`,避免任何边界条件(也体现了「令牌」语义:它本身不该被守卫逻辑干预)。

**练习 2**:如果有人在新代码里写了 `server_args.attention_backend = "triton"`(已 materialize 后),会发生什么?正确做法是什么?

**参考答案**:会抛 `AttributeError`(守卫拦截)。正确做法是 `server_args.override("your_source", attention_backend="triton")`;若要让 `get_attention_backends()` 等命名空间读者看到,则用 `get_context().override("your_source", attention_backend="triton")`。

---

### 4.4 启动链路与 http_server.launch_server

#### 4.4.1 概念说明

前面三个模块讲清了「配置对象本身」。本模块把它们放进真实的启动链路里:**命令行 → `ServerArgs` 构造(触发 `__post_init__` 解析+materialize)→ 模式分发 → `launch_server` 拉起多进程**。

这条链路的关键点:`prepare_server_args` 内部调用 `ServerArgs.from_cli_args(raw_args)`,而 dataclass 的构造会自动触发 `__post_init__`——也就是说,**4.1/4.2/4.3 描述的解析、装配、只读化,全部发生在 `prepare_server_args` 这一步里**,早于任何子进程被拉起。这意味着传给 `launch_server` 的 `server_args` 已经是「只读、已解析」的快照。

#### 4.4.2 核心流程

完整调用链(伪代码):

```
sglang serve <model> [opts]            # 控制台脚本 -> cli.main:main -> serve()
  serve(args, extra_argv):             # cli/serve.py
    load_plugins()
    _extract_model_type_override(...)  # auto/llm/diffusion
    _normalize_positional_model_path(...)  # 允许 `sglang serve <model>`
    model_path = get_model_path(...)
    if 扩散模型:  execute_serve_cmd(...)        # 另一条路
    else (语言模型):
        server_args = prepare_server_args(argv)  # ★ 构造 ServerArgs,触发 __post_init__
        run_server(server_args)                   # 模式分发
  run_server(server_args):            # launch_server.py
    if encoder_only:  encode_server.launch_server(...)
    elif smg_grpc_mode: serve_grpc(...)
    elif use_ray:      ray.http_server.launch_server(...)
    else:              http_server.launch_server(server_args)   # ★ 默认
  launch_server(server_args):         # entrypoints/http_server.py
    Engine._launch_subprocesses(...)  # 拉起 TokenizerManager/Scheduler/Detokenizer
    _setup_and_run_http_server(...)   # 启动 FastAPI(lifespan 里再起 gRPC/sidecar)
```

注意 `serve()` 用 `try/finally + kill_process_tree` 兜底回收派生的子进程([serve.py:143](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/cli/serve.py#L143))。

#### 4.4.3 源码精读

入口分流 —— 语言模型分支:

[python/sglang/cli/serve.py:56-105](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/cli/serve.py#L56-L105) —— `serve(args, extra_argv)` 的前半段:加载插件、抽取 `--model-type`、归一化位置式模型路径。`_extract_model_type_override`(定义于 [serve.py:16](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/cli/serve.py#L16))把 `auto/llm/diffusion` 从 argv 里摘出来单独处理。

[python/sglang/cli/serve.py:134-143](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/cli/serve.py#L134-L143) —— 语言模型分支:`prepare_server_args(dispatch_argv)` 构造配置,`run_server(server_args)` 启动;`finally` 里 `kill_process_tree` 回收子进程。

配置构造 —— `prepare_server_args`(注意它触发了整个解析管线):

[python/sglang/srt/server_args.py:8657-8693](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L8657-L8693) —— 建 argparse、`ServerArgs.add_cli_args(parser)`、处理 `--config` 合并、`parser.parse_args`、配置基本日志(让 `__post_init__` 里的 `logger.info/warning` 有格式),最后 `ServerArgs.from_cli_args(raw_args)` 构造并返回。正是这一行触发 `__post_init__`,进而执行 4.2 的全部解析与 `materialize_declarations`。

模式分发 —— `run_server`:

[python/sglang/launch_server.py:15-52](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/launch_server.py#L15-L52) —— 按 `encoder_only` / `smg_grpc_mode` / `use_ray` / 默认四条路分发,默认走 `sglang.srt.entrypoints.http_server.launch_server`。

默认模式落点 —— `launch_server`:

[python/sglang/srt/entrypoints/http_server.py:2657-2703](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/http_server.py#L2657-L2703) —— 文档字符串(第 2665-2679 行)清楚说明了三进程结构(TokenizerManager 在主进程;Scheduler、DetokenizerManager 为子进程;ZMQ IPC)。函数体调用 `Engine._launch_subprocesses(...)` 拉起子进程,再 `_setup_and_run_http_server(...)` 启 FastAPI。函数签名里那些 `init_tokenizer_manager_func` 等可注入回调,是为了让不同部署形态(如 Ray)能替换子进程工厂。

> 提示:`launch_server` 本身只拉子进程与启 HTTP;真正「在服务运行期间」随生命周期起停的东西(原生 gRPC server、sidecar)发生在 FastAPI 的 `lifespan` 里,见 4.5。

#### 4.4.4 代码实践

**实践目标**:把本讲前四个模块串成一条「配置从命令行到运行时」的完整链路。

**操作步骤**:

1. 从 [serve.py:134-143](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/cli/serve.py#L134-L143) 出发,跳到 `prepare_server_args`([server_args.py:8657-8693](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L8657-L8693))。
2. 在 `prepare_server_args` 末尾的 `ServerArgs.from_cli_args(raw_args)` 处,意识到这会进入 `__post_init__`([server_args.py:3283-3488](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L3283-L3488)),跑完所有 `_handle_*` 并在末尾 `materialize_declarations(self)`,此后 `server_args` 只读。
3. 返回到 `run_server`([launch_server.py:15-52](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/launch_server.py#L15-L52)),确认默认分支进入 `http_server.launch_server`([http_server.py:2657-2703](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/http_server.py#L2657-L2703))。
4. 画一张纵向时序图,标注「解析+只读化」发生在 `launch_server` 拉子进程**之前**。

**需要观察的现象**:配置解析与只读化都集中在 `prepare_server_args` 这一步;之后所有进程拿到的都是同一份只读快照。

**预期结果**:你能指出——`launch_server` 收到的 `server_args` 已是「已解析、只读」的;任何运行期改写都必须走 `override()` / `get_context().override()`,这条约束从启动链路的最早期就生效。

**待本地验证**:若环境允许,可用 `sglang serve --model-path <小模型> --tp 2` 启动并在日志里观察 `__post_init__` 打印的 `logger.info`(如注意力后端自动选择),体会解析管线确实在 `launch_server` 拉子进程前就跑完了。

#### 4.4.5 小练习与答案

**练习 1**:`run_server` 为什么要在 `encoder_only`/`smg_grpc_mode`/`use_ray`/默认 之间分发?它们和 `launch_server` 是什么关系?

**参考答案**:不同部署形态(编码分离、gRPC、Ray、默认 HTTP)需要不同的进程拓扑与服务协议,因此 `run_server` 是一个模式分发器;默认分支才进入本讲的 `http_server.launch_server`。所有分支都共享同一份已解析的 `server_args`。

**练习 2**:`launch_server` 拉起的子进程,各自也会再构造一份 `ServerArgs` 吗?如果是,这意味着什么?

**参考答案**:是的,子进程通常会重新解析出自己的 `ServerArgs`(可能从序列化的启动参数重建)。这意味着「只读、可审计、声明式解析」这套设计在每个进程里都成立——这也是为什么运行期改写必须走统一入口(让各进程配置可对齐),而不能各自裸赋值。

---

### 4.5 sidecar:serving 命名空间的本地 gRPC 旁路进程

> 这是本轮(提交 `21065bc8`,PR #31076)新增的功能。它正好把 4.1 的命名空间、4.2 的 `__post_init__` 校验、4.4 的 `http_server` lifespan 三个机制在一个真实特性上串起来,所以放在最后作为综合例证。

#### 4.5.1 概念说明

SGLang 有一套**原生 gRPC server**:用 `--grpc-port`(或环境变量 `SGLANG_GRPC_PORT`)开启,与旧式的 `--smg-grpc-mode`/`--grpc-mode`(SMG gRPC)区分。在默认 HTTP 模式下,这个原生 gRPC server 由 `http_server` 的 FastAPI `lifespan` 负责拉起(函数 `_start_native_grpc_server_for_runtime`)。

**sidecar(旁路进程)** 解决的问题是:有时你需要在 SGLang 主进程旁边再跑一个辅助进程——例如一个把外部协议翻译成原生 gRPC 调用的适配器、一个聚合/代理层,或一个观测/注入组件。与其让用户自己在 systemd 里管理这个进程,SGLang 提供了一个**本地托管的 sidecar 启动器**:

- 你只需在命令行指定一个**可导入的 Python 模块名**(`--sidecar MODULE`),该模块必须暴露一个 `main(argv)` 函数;
- SGLang 用 `multiprocessing.spawn` 拉起这个模块;
- 并把**解析后的原生 gRPC 端点**通过环境变量 `SGLANG_GRPC_ENDPOINT` 传给它(模块的 `main` 从这里读到该连谁)。

这样,sidecar 的「发现 + 端点对接 + 生命周期托管」就被纳入了 `ServerArgs` 配置模型:两个字段 `sidecar`/`sidecar_args` 归入 `serving` 命名空间(4.1),在 `__post_init__` 做约束校验(本模块),在 `http_server` lifespan 里随主服务一起启动/停止(本模块)。

#### 4.5.2 核心流程

「配置 → 校验 → 启动 → 托管 → 关闭」五段:

```
配置(CLI / ServerArgs 字段):
    --sidecar MODULE              # 必填,可导入的模块名,暴露 main(argv)
    --sidecar-args '["..."]'      # 可选,传给 main(argv) 的 JSON 字符串数组
    --grpc-port N                 # 必填,sidecar 必须有原生 gRPC server 可对接

校验(__post_init__,早于任何子进程):
    sidecar_args 非空 => 必须配 sidecar,且必须是字符串 JSON 数组
    sidecar 非空    => 不能为空串;不能与 legacy gRPC 模式共存;必须有 grpc_port

启动(http_server lifespan):
    _start_native_grpc_server_for_runtime(...)   # 先起原生 gRPC server
    if server_args.sidecar is not None:
        sidecar = start_sidecar(server_args)     # 再 spawn sidecar 子进程

sidecar 子进程内部(_run_sidecar):
    设置 SGLANG_GRPC_ENDPOINT=<回环host>:<grpc_port>
    importlib.import_module(module).main(argv)

托管 / 关闭(lifespan finally):
    sidecar.stop()   # terminate + join(受 --sidecar-shutdown-timeout 控制),超时则 kill_process_tree
```

关键设计点:sidecar 只对接**原生** gRPC server,所以它和 legacy 的 `--smg-grpc-mode` 互斥;且必须有 `--grpc-port`,否则没有端点可连。端点地址用**回环地址**(`build_sidecar_endpoint` 把 `0.0.0.0`/`::` 折回 `127.0.0.1`/`::1`),因为 sidecar 与主进程同机。

#### 4.5.3 源码精读

**(1) 字段定义** —— 归入 `serving` 命名空间,正是 4.1 三段式的又一个实例:

[python/sglang/srt/server_args.py:1185-1202](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L1185-L1202) —— `sidecar` 用裸字符串帮助文本(说明模块需暴露 `main(argv)` 且读 `SGLANG_GRPC_ENDPOINT`、需 `--grpc-port`),`sidecar_args` 用 `Arg(type_parser=json_list_type)`(说明是 JSON 数组,且 `--sidecar-shutdown-timeout` 会被 SGLang 消费),两者末尾都是 `NS("serving")`。

**(2) `__post_init__` 校验** —— 这是「声明只读模型」之外的另一类 `__post_init__` 职责:纯校验(只读 + raise),不写任何字段:

[python/sglang/srt/server_args.py:3749-3768](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L3749-L3768) ——
- L3752-3758:`sidecar_args` 非空时,要求同时有 `sidecar`,且必须是「字符串组成的 list」(`json_list_type` 已解析成 list,这里再做元素类型断言)。
- L3759-3768:`sidecar` 非空时,三道校验:① 非空串;② 不能与 legacy gRPC(`smg_grpc_mode`/`grpc_mode`)共存;③ 必须有 `grpc_port`(否则报 `--sidecar requires --grpc-port or SGLANG_GRPC_PORT`)。
- 这段紧跟在 `native_grpc = self.grpc_port is not None and not legacy_grpc`(L3751)之后,与原生 gRPC 的其它互斥校验(如不能配 `--use-ray`/`--encoder-only`)同处一个校验块。

**(3) lifespan 启动与停止** —— sidecar 与原生 gRPC server 同生共死:

[python/sglang/srt/entrypoints/http_server.py:401-404](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/http_server.py#L401-L404) —— 在 `_start_native_grpc_server_for_runtime(...)`([http_server.py:395-400](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/http_server.py#L395-L400))拿到 `grpc_handle` 之后,若 `server_args.sidecar is not None`,则 `from ...sidecar import start_sidecar; sidecar = start_sidecar(server_args)`。注意 `sidecar` 这个局部变量在 lifespan 开头就被初始化为 `None`([http_server.py:266-269](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/http_server.py#L266-L269)),保证下面的 `finally` 总能安全引用它。

[python/sglang/srt/entrypoints/http_server.py:416-420](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/http_server.py#L416-L420) —— `finally` 里 `sidecar.stop()`(包了 try/except 记日志),且**早于** `_shutdown_native_grpc_server(grpc_handle)`(L421),顺序上先停 sidecar 再停 gRPC。

**(4) sidecar 启动器本体** —— 新文件 `entrypoints/sidecar.py`:

- [sidecar.py:39-42](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/sidecar.py#L39-L42) —— `build_sidecar_endpoint(server_args)`:把 `host` 折成回环地址(借 [sidecar.py:31-36](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/sidecar.py#L31-L36) 的 `_loopback_host`)后,与 `grpc_port` 拼成端点 URL。
- [sidecar.py:45-55](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/sidecar.py#L45-L55) —— `_parse_sidecar_args`:从 `sidecar_args` 里**先用 argparse 摘走** `--sidecar-shutdown-timeout`(默认 45 秒,见 [sidecar.py:28](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/sidecar.py#L28)),剩下的才作为 `provider_args` 原样传给模块的 `main`。
- [sidecar.py:58-75](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/sidecar.py#L58-L75) —— `_run_sidecar`:这是 spawn 到子进程里的 target。它先 `kill_itself_when_parent_died()`(父进程没了就自杀,避免孤儿),把端点写进 `SGLANG_GRPC_ENDPOINT` 环境变量,然后 `importlib.import_module(module)` 取出 `main` 并调用 `main(args)`。模块不可导入或没有 `main` 会抛清晰的 `RuntimeError`。
- [sidecar.py:78-111](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/sidecar.py#L78-L111) —— `Sidecar` 类:持有进程 + `SubprocessWatchdog`。`start()` 启进程与看门狗;`stop()` 先 `terminate + join(shutdown_timeout)`,仍存活则 `kill_process_tree` 兜底。
- [sidecar.py:114-130](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/sidecar.py#L114-L130) —— `start_sidecar(server_args)`:解析参数、算端点、用 `mp.get_context("spawn").Process(...)` 创建子进程(target=`_run_sidecar`),包成 `Sidecar` 并 `.start()`。用 **spawn** 而非 fork,保证子进程拿到干净的解释器状态(与 CUDA 等扩展更兼容)。

#### 4.5.4 代码实践

**实践目标**:把 sidecar 从「字段 → 校验 → 启动 → 关闭」完整走一遍(源码阅读型;真跑起来需要一个可导入的 sidecar 模块 + 原生 gRPC 环境)。

**操作步骤**:

1. 在 [server_args.py:1185-1202](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L1185-L1202) 确认两个字段的 `NS("serving")`(承接 4.1)。
2. 在 [server_args.py:3749-3768](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L3749-L3768) 列出 `__post_init__` 对 sidecar 的全部约束(共 4 条 raise)。
3. 在 [http_server.py:401-404](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/http_server.py#L401-L404) 与 [http_server.py:416-420](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/http_server.py#L416-L420) 确认 sidecar 的启停都挂在 lifespan 上、且与原生 gRPC server 同生命周期。
4. 在 [sidecar.py:114-130](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/sidecar.py#L114-L130) 追踪 `start_sidecar` → spawn → `_run_sidecar`([sidecar.py:58-75](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/sidecar.py#L58-L75))的调用链,标注 `SGLANG_GRPC_ENDPOINT` 在哪一行被设置、`main(argv)` 在哪一行被调用。

**需要观察的现象**:校验全在 `__post_init__`(早于子进程);启动/停止全在 lifespan(运行期);sidecar 子进程通过环境变量拿到端点,而非命令行参数。

**预期结果**:你能指出——「配置在 `prepare_server_args` 阶段就被校验好了(`__post_init__`),所以传进 lifespan 的 `server_args` 已经是合法的;sidecar 只是按这份只读配置去 spawn 一个外部模块」。这恰好印证 4.3 的「运行期不裸改、只读快照」:sidecar 读的是 `server_args.sidecar`/`grpc_port`,从不写它。

**待本地验证**:写一个最小 sidecar 模块(例如 `my_sidecar.py` 含 `def main(argv): import os; print("connect to", os.environ["SGLANG_GRPC_ENDPOINT"]); time.sleep(60)`),然后:

```bash
sglang serve --model-path <小模型> --grpc-port 50051 \
    --sidecar my_sidecar --sidecar-args '[]'
```

预期:服务启动后 sidecar 子进程打印出 `connect to 127.0.0.1:50051`;Ctrl-C 停服时 sidecar 被一并回收。若本地无 GPU/依赖,则标注「待本地验证」,改为纯源码阅读完成上述 4 步。

#### 4.5.5 小练习与答案

**练习 1**:为什么 `--sidecar` 必须要求 `--grpc-port`,且不能与 `--smg-grpc-mode` 共存?

**参考答案**:sidecar 是对接**原生** gRPC server 的旁路进程,它从 `SGLANG_GRPC_ENDPOINT` 读取端点,而该端点来自 `grpc_port`。没有 `grpc_port` 就没有原生 gRPC server 可连;而 `--smg-grpc-mode`/`--grpc-mode` 走的是 legacy SMG gRPC 路径(不经 `_start_native_grpc_server_for_runtime`),不会产出 sidecar 期望的端点,所以二者互斥。

**练习 2**:`start_sidecar` 为什么用 `multiprocessing.get_context("spawn")` 而不是默认 fork?

**参考答案**:fork 会把父进程(已加载 CUDA/torch 等扩展、可能持有 GPU 上下文与线程)的内存原样复制给子进程,极易导致 CUDA/线程状态损坏。spawn 启动全新解释器,只重新 import 指定模块,状态干净,更适合在 GPU 服务进程旁边拉起一个独立的 sidecar。

**练习 3**:sidecar 的启停为什么放在 FastAPI 的 `lifespan` 里,而不是 `launch_server` 里?

**参考答案**:`lifespan` 对应「应用启动完成 ~ 应用关闭」这段真正的服务运行期;原生 gRPC server 也在此时段内由 `_start_native_grpc_server_for_runtime` 拉起。把 sidecar 放在 lifespan、紧跟 gRPC server 之后,能保证 sidecar 启动时 gRPC 端点已就绪,且在 `finally` 里先于 gRPC server 关闭,做到同生命周期、无孤儿进程。

---

## 5. 综合实践

**任务:绘制「配置生命周期一张图」,并定位 sidecar 这条新链路上的关键代码。**

请综合本讲五个模块,完成:

1. **画一张配置生命周期流程图**,包含以下阶段,并标注每个阶段对应的源码位置:
   - 命令行参数 → `prepare_server_args` 构造 `ServerArgs`
   - `__post_init__` 解析(`_handle_*` 序列 + sidecar 等校验,声明累积进 `_resolved_overrides`)
   - `materialize_declarations` 一次性应用 → `_declarations_materialized = True`
   - `run_server` 分发 → `http_server.launch_server` 拉子进程
   - lifespan 内:起原生 gRPC server → `start_sidecar`(若配了 `--sidecar`)
   - 运行期改写只能走 `override()` / `get_context().override()`

2. **在图上标出「只读边界」**:即 `materialize_declarations` 调用处([server_args.py:3488](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L3488))——这之前字段可被解析管线写,这之后只读。

3. **回答四个问题**(用本讲源码佐证):
   - `mem_fraction_static` 属于哪个命名空间?为什么和它的代码段落名不一致?
   - 模型代码想为某架构自动设置 `attention_backend`,正确写法是 `server_args.attention_backend = "x"` 还是返回 `{"attention_backend": "x"}`?为什么?
   - 运行期直接 `server_args.tp_size = 8` 会发生什么?正确做法是什么?
   - 给出 sidecar 从「字段」到「子进程 `main(argv)` 被调用」的最短调用链(至少 4 个函数/方法),并指出端点是通过什么环境变量传递的。

**验收标准**:流程图能清晰体现「声明-应用分离」「只读边界」「唯一改写入口」三个核心不变量,并正确画出 sidecar 这条「配置→校验→lifespan 启停」支线;四个问题都能引用到本讲给出的具体源码行号。

---

## 6. 本讲小结

- `ServerArgs` 是配置源头:字段用 `A[T, Arg(...), NS("命名空间")]` 三段式标注,`NS` 标记逻辑归属(与字段在文件里的物理位置无关,如 `mem_fraction_static` 属 `schedule` 而非 `memory`,新字段 `sidecar`/`sidecar_args` 属 `serving`)。
- 配置解析在 `__post_init__` 里以「有序 dispatcher」(`_handle_*`)完成;其中既有「声明式覆盖」,也有「纯校验」(如 sidecar 的互斥/必填检查);模型代码**声明**要改什么(返回 dict),不直接写字段。
- 声明累积进 `_resolved_overrides` stash,直到 `materialize_declarations` 在 `__post_init__` 末尾**按 last-writer-wins 一次性应用**,并置 `_declarations_materialized = True`。
- 应用之后 `ServerArgs` 只读:重写的 `__setattr__` 在「非下划线 + 已 materialize + 非 override 上下文」三条件成立时直接 raise。
- 运行期改写的唯一合法入口是 `override(source, **fields)`,它用 `_in_override` 令牌绕过守卫、并把改动记进 stash(可重放)和 mutations 日志(可追溯);要让命名空间读者看到则走 `get_context().override()`。
- 整条链路:`sglang serve` → `serve()` → `prepare_server_args()`(触发解析+校验+只读化)→ `run_server()` 分发 → `http_server.launch_server()` 拉起三进程;原生 gRPC server 与 sidecar 则在 FastAPI `lifespan` 里随服务同生共死。
- sidecar 是本轮新增的真实例证:`--sidecar MODULE` 指定一个暴露 `main(argv)` 的模块,SGLang 用 spawn 拉起它、通过环境变量 `SGLANG_GRPC_ENDPOINT` 传递原生 gRPC 端点,并把启停纳入 lifespan;它必须配 `--grpc-port`、不能与 legacy gRPC 共存——这些约束全部在 `__post_init__` 早早就位。

---

## 7. 下一步学习建议

- **u2-l5(RuntimeContext 与配置命名空间)**:本讲的 `namespace_of(ServerArgs)` 产出的字段→命名空间映射,正是 `RuntimeContext` 构建「配置袋(config bags)」的输入;`get_parallel()`/`get_schedule()`/`get_serving()` 等访问器就是把袋里的字段暴露给各子系统读。本讲新加的 `serving` 命名空间字段(`sidecar`/`sidecar_args`)也会被归入 `serving` 袋。强烈建议紧接着学。
- **u2-l3(请求端到端流转)**:理解了配置只读模型后,再看一条请求在 TokenizerManager→Scheduler→Detokenizer 之间如何流转,会清楚它们各自如何读取这份只读配置。
- **延伸阅读**:若想深入「声明式覆盖」,可通读 [overrides.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/arg_groups/overrides.py) 中的 `@register_model_override` 派生覆盖族(如 `_mimo_v2_overrides`)与 `@register_post_process` 后处理 pass(如本轮为 `hpc_ops` 后端新增 `page_size=64` 约束的 `_mla_backend_page_constraints`),体会「忠实搬运旧条件逻辑、改为返回声明」的迁移手法;若对 sidecar 感兴趣,可对照 [sidecar.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/entrypoints/sidecar.py) 阅读原生 gRPC server 的启动函数 `_start_native_grpc_server_for_runtime`。
