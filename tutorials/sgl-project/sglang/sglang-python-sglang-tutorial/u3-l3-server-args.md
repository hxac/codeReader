# ServerArgs 配置体系

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `ServerArgs` 作为「运行时单一配置源」的设计意图，以及它如何从一行 `sglang serve ...` 命令一路演变成一个被所有子进程读取的冻结对象。
- 看懂「注解式声明」：一个字段（如 `tp_size`）只用一行 `A[int, Arg(...), NS(...)]`，就能同时表达数据类型、CLI 选项、帮助文本、命名空间分组。
- 追踪 `prepare_server_args` 的解析全流程：argparse 解析 → YAML 配置合并 → `__post_init__` 派生校验 → 冻结只读。
- 理解 `arg_groups/` 这个「分组钩子」目录如何把不同特性（PD 分离、投机解码、HiSparse、模型专属覆盖）的解析逻辑从 8800 行的 `server_args.py` 中抽离出去。
- 记住一条铁律：`__post_init__` 执行完之后，`ServerArgs` 字段是**只读**的，运行期要改配置必须走唯一的 `override()` 入口。

本讲承接 u3-l1（服务启动全流程），上探到「参数从哪来、怎么被消费」；下接 u3-l4（RuntimeContext），后者正是基于本讲的 `NS(...)` 命名空间把字段组织成上下文树。

## 2. 前置知识

- **数据类（dataclass）**：Python 的 `@dataclasses.dataclass` 装饰器能根据类上的类型注解自动生成 `__init__`。本讲会大量接触 `ServerArgs` 这个超大数据类，它有数百个字段。
- **`typing.Annotated`**：标准库提供的一种「给类型打标签」的写法。`Annotated[int, "一段说明"]` 的类型仍是 `int`，但额外携带了一段元数据。SGLang 把它简写成 `A`，用来把「CLI 参数描述」直接挂在字段上。
- **argparse**：Python 标准库的命令行解析器。`add_argument("--tp-size", type=int, default=1, ...)` 是它的基本用法。
- **「单一配置源（single source of truth）」**：一种工程约定——程序的所有配置都从同一个对象读取，避免散落在各处的全局变量或环境变量里造成不一致。`ServerArgs` 就是 SGLang 的单一配置源。
- **进程拓扑回顾（来自 u3-l2）**：主进程跑 `TokenizerManager`，子进程跑 `Scheduler` 和 `DetokenizerManager`。这三方都需要读取同一份配置，所以 `ServerArgs` 必须能在进程间被完整序列化传递。

> 术语提示：项目规则（`.claude/rules/no-dataclasses.md`）要求新代码用 `msgspec.Struct` 而非 `@dataclass`。但 `ServerArgs` 是历史遗留的超大类，被显式「豁免（grandfathered）」，只在编辑相关字段时顺带迁移。所以本讲你会看到 `@dataclasses.dataclass`，这是预期内的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [srt/server_args.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py) | 8800+ 行的核心文件。定义 `ServerArgs` 数据类（字段声明 + `__post_init__` 派生校验）、`prepare_server_args` 解析入口、以及 `PortArgs`（进程间通信端口名）。 |
| [srt/server_args_config_parser.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args_config_parser.py) | `ConfigArgumentMerger`：把 YAML 配置文件与命令行参数合并，确立 `CLI > Config > Defaults` 的优先级。 |
| [srt/arg_groups/arg_utils.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/arg_utils.py) | `A`/`Arg`/`NS` 元数据定义，以及 `add_cli_args_from_dataclass`——从字段注解**自动派生** argparse 参数。 |
| [srt/arg_groups/](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/) | 「分组钩子」目录：`speculative_hook.py`、`pd_disaggregation_hook.py`、`hisparse_hook.py`、`overrides.py`（模型覆盖注册表）、`argparse_actions.py`（弃用动作）等。 |

## 4. 核心概念与源码讲解

### 4.1 ServerArgs 数据类与注解式参数声明

#### 4.1.1 概念说明

`ServerArgs` 是一个普通的数据类，但它承载了 SGLang **所有**的服务参数：模型路径、张量并行度、KV 缓存占比、上下文长度、PD 分离模式、投机解码算法、量化方式……可以说，运行时里几乎每一处行为都能在一个 `ServerArgs` 字段上找到开关。

它的特别之处不在于「字段多」，而在于**声明方式**。传统写法是「先写字段、再单独写一大段 `add_argument`」，两处容易不一致（改了字段忘了改 CLI）。SGLang 采用**注解式声明**：字段的类型注解里同时塞进 CLI 描述，再用一个通用函数把注解自动翻译成 argparse 参数，做到「字段即 CLI」。

`ServerArgs` 类的开头有一段写给贡献者的文档，直接说明了这套规则：

[srt/server_args.py:412-452](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L412-L452) — 类定义与「如何新增参数」的文档，规定新字段必须用 `A[T, ...]` 注解，主 CLI 名从字段名自动推导（`tp_size` → `--tp-size`）。

#### 4.1.2 核心流程

一个字段从声明到可用的旅程：

1. **声明**：在类体里写 `字段名: A[类型, 元数据...] = 默认值`。
2. **派生 CLI**：`add_cli_args_from_dataclass` 扫描所有字段，按类型推导出 `--flag`。
3. **解析**：用户在命令行传 `--flag value`，argparse 解析进 `Namespace`。
4. **构造**：`from_cli_args` 把 `Namespace` 喂给数据类构造器，触发 `__post_init__`。
5. **派生+校验+冻结**：`__post_init__` 串行调用一堆 `_handle_*`，最后冻结为只读。

理解注解式声明，关键是三个名字：`A`、`Arg`、`NS`。

#### 4.1.3 源码精读

**`A` 就是 `typing.Annotated` 的别名**（[srt/arg_groups/arg_utils.py:58](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/arg_utils.py#L58)），`Arg` 是一个 frozen（不可变）数据类，承载 CLI 元数据：

[srt/arg_groups/arg_utils.py:61-83](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/arg_utils.py#L61-L83) — `Arg` 定义：`help`（帮助文本）、`choices`（可选值）、`aliases`（别名，如 `--model`）、`cli_name`（自定义 CLI 名）、`type_parser`（自定义类型转换，如支持 `32k` 这样的可读整数）、`nargs`、`action`、`no_cli`（标记「不出现在 CLI 上，仅 Python 内部用」）、`resolvable`（标记「允许被覆盖管线写入」，见 4.4）。

`NS` 则是**命名空间标记**，与 CLI 无关，专门给 u3-l4 的 `RuntimeContext` 用来分组：

[srt/arg_groups/arg_utils.py:85-98](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/arg_utils.py#L85-L98) — `NS` 把一个字段归入某个点分命名空间（如 `NS("parallel")`），`namespace_of` 据此构建上下文配置树。

下面看三个真实字段的声明，体会三种典型写法：

**① 最简形式——裸字符串当帮助文本**（`mem_fraction_static`）：

[srt/server_args.py:722-726](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L722-L726) — KV 缓存静态分配占比，默认 `None`（`__post_init__` 里再根据显存推算），归入 `NS("schedule")` 命名空间。

**② 带别名的标量**（`tp_size`）：

[srt/server_args.py:953-960](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L953-L960) — 张量并行度，主 CLI 名自动推导为 `--tp-size`，别名 `--tensor-parallel-size`，默认 `1`，归入 `NS("parallel")`。

**③ 带自定义类型解析器的可选字段**（`context_length`）：

[srt/server_args.py:529-537](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L529-L537) — 上下文长度，默认 `None`（用模型 `config.json` 里的值），`type_parser=human_readable_int` 让你在命令行写 `--context-length 32k` 这样可读的整数。

**自动派生的核心函数 `add_cli_args_from_dataclass`** 按字段类型分派出不同的 argparse 形态：

[srt/arg_groups/arg_utils.py:218-337](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/arg_utils.py#L218-L337) — 遍历字段，按如下规则推导：`Literal[...]` → `choices`；`List[X]` → `nargs="+"`；`bool` → `action="store_true"`；其余标量 → `type=` 推断（见下面的关键片段）。

其中 `bool` 字段自动变成 `store_true` 开关：

[srt/arg_groups/arg_utils.py:313-319](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/arg_utils.py#L313-L319) — 布尔字段被注册成「出现即为 True」的开关。这就是为什么你在命令行只写 `--skip-tokenizer-init` 而不写 `--skip-tokenizer-init true`。

> 设计要点：注解式声明让「加一个新参数」的成本降到一行——加字段、写帮助文本、设默认值，CLI 自动就有了。这把 `ServerArgs` 从「最难维护的文件」变成了「相对好维护的文件」。

#### 4.1.4 代码实践

1. **实践目标**：用 `dummy` 模型构造一个 `ServerArgs`，**不启动 GPU**，直接观察默认值与注解式声明。
2. **操作步骤**：

   ```bash
   cd python/sglang
   python -c "
   from sglang.srt.server_args import ServerArgs
   # model_path='dummy' 会触发 __post_init__ 的早退分支，跳过所有 GPU/模型相关派生
   sa = ServerArgs(model_path='dummy')
   print('tp_size =', sa.tp_size)
   print('mem_fraction_static =', sa.mem_fraction_static)  # dummy 模式下仍是 None
   print('context_length =', sa.context_length)
   "
   ```

3. **需要观察的现象**：三条 `print` 应分别输出 `tp_size = 1`、`mem_fraction_static = None`、`context_length = None`。
4. **预期结果**：能成功构造且无 GPU 依赖，证明 `dummy` 短路生效（`__post_init__` 在 [srt/server_args.py:3322-3323](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L3322-L3323) 处 `return`）。
5. 这是「源码阅读型实践」，可离线运行；若 `import sglang.srt` 因缺依赖失败，请先 `pip install "sglang[all]"`。

#### 4.1.5 小练习与答案

**练习 1**：你想新增一个布尔参数 `enable_foo`，命令行写作 `--enable-foo`，默认关闭。最少需要写几行？

**答案**：一行字段声明即可：`enable_foo: A[bool, "Enable foo feature."] = False`。`add_cli_args_from_dataclass` 会自动把它注册成 `--enable-foo` 的 `store_true` 开关，无需手写 `add_argument`。

**练习 2**：`A[int, "help"]` 里的那个字符串，最终用在了哪里？

**答案**：被 `_unwrap_annotated`（[srt/arg_groups/arg_utils.py:147-163](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/arg_utils.py#L147-L163)）当作 `Arg(help=...)`，最终传给 argparse 的 `help=`，即出现在 `sglang serve --help` 的输出里。

---

### 4.2 prepare_server_args：命令行解析全流程

#### 4.2.1 概念说明

`prepare_server_args` 是「命令行字符串 → `ServerArgs` 对象」的唯一入口，在 u3-l1 讲过的启动链路里，它紧跟在 CLI 分发之后被调用（`cli/serve.py` → `launch_server.run_server` → `prepare_server_args`）。

它要解决三件事：

1. **解析**：把 `sys.argv` 里的字符串按 argparse 规则解析。
2. **合并配置文件**：如果用户传了 `--config xxx.yaml`，要把 YAML 里的参数和命令行参数合并，且命令行优先。
3. **构造对象**：把解析结果交给 `ServerArgs.from_cli_args`，触发 `__post_init__`。

#### 4.2.2 核心流程

参数优先级是本节最关键的结论——**CLI > Config（YAML）> Defaults**：

```text
sys.argv:  ["--port", "8000", "--config", "my.yaml", "--tp-size", "2"]
                                  │
                ┌─────────────────┴──────────────────┐
                │ ConfigArgumentMerger.merge_config_with_args
                │   1. 读 my.yaml → 转成 ["--xxx", ...] 列表
                │   2. 拼接顺序：config_args + 命令行在 --config 之前的参数 + 之后的参数
                │   （命令行参数整体排在 config 之后，故后出现者覆盖前者）
                └─────────────────┬──────────────────┘
                                  ▼
            parser.parse_args(argv)   → argparse.Namespace
                                  ▼
            ServerArgs.from_cli_args(raw_args)  → __post_init__ 派生/校验/冻结
                                  ▼
                              ServerArgs 对象
```

为什么「排在后面」就「优先」？因为 argparse 对同一个 `dest`，后解析的值覆盖先解析的值。合并器故意把 YAML 参数放在前面、命令行参数放在后面，从而让命令行永远赢。

#### 4.2.3 源码精读

入口函数全貌（仅 37 行）：

[srt/server_args.py:8682-8718](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L8682-L8718) — 创建 `argparse.ArgumentParser(prog="sglang serve")`，调用 `ServerArgs.add_cli_args(parser)` 注册所有选项；若检测到 `--config`，则用 `ConfigArgumentMerger` 先合并；然后 `parse_args`、配置 logging、最后 `from_cli_args`。

`add_cli_args` 本身很薄——绝大多数参数由自动派生完成，只剩三类需要手工注册：

[srt/server_args.py:7588-7603](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L7588-L7603) — `add_cli_args` 第一行就是 `add_cli_args_from_dataclass(parser, ServerArgs)`（自动派生），之后才是手工注册的「动态 choices」参数（如 `--reasoning-parser`，其可选值来自插件注册表，运行时才能确定）。

`--config` 这个元参数不是数据类字段，必须手工注册：

[srt/server_args.py:7627-7632](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L7627-L7632) — 注册 `--config`，帮助文本说明它必须是一个 YAML 文件。

`from_cli_args` 负责把 `Namespace` 转成数据类实例，并贴心地跳过「故意没有 CLI 表面」的字段：

[srt/server_args.py:7830-7837](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L7830-L7837) — 只取那些在 `Namespace` 上确实存在的字段（用 `hasattr` 过滤），这样标记了 `no_cli=True` 的字段会自动回退到数据类默认值。

**配置合并器**是优先级的关键。看它的构造与合并逻辑：

[srt/server_args_config_parser.py:17-50](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args_config_parser.py#L17-L50) — `ConfigArgumentMerger` 在构造时从 parser 上扫描出所有 `store_true` 动作和「不支持的动作」（除 `store_true`/`store` 之外的动作，配置文件不允许设置它们）。

[srt/server_args_config_parser.py:52-83](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args_config_parser.py#L52-L83) — 合并主逻辑：找到 `--config` 的位置，把参数切成三段——`config_args + before_config + after_config`。**注意顺序**：`config_args` 在最前，命令行参数整体在后，所以命令行覆盖配置文件。

布尔参数的特殊处理尤其值得看，因为 `store_true` 开关不能写 `--flag false`：

[srt/server_args_config_parser.py:162-177](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args_config_parser.py#L162-L177) — 对 `store_true` 开关：YAML 里写 `true` 就追加该 flag，写 `false` 就跳过（不能输出 `--flag false`，那会被 argparse 当成位置参数）；对普通布尔则正常输出 `--flag true/false`。

> 设计要点：合并器把 YAML 和 CLI 统一成「一串 argv」，复用同一套 argparse 规则，避免了「YAML 解析器」和「CLI 解析器」两套逻辑各走各的导致不一致。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 `CLI > Config > Defaults` 的优先级，无需启动服务。
2. **操作步骤**：
   ```bash
   cd python/sglang
   # 写一个最小 YAML
   cat > /tmp/sgl.yaml <<'EOF'
   port: 9000
   tp-size: 4
   EOF
   # 用 prepare_server_args 直接解析（dummy 模型短路，不碰 GPU）
   python -c "
   from sglang.srt.server_args import prepare_server_args
   # 场景 A：只给 YAML
   sa_a = prepare_server_args(['--model-path','dummy','--config','/tmp/sgl.yaml'])
   print('A (yaml only)        port =', sa_a.port, 'tp =', sa_a.tp_size)
   # 场景 B：YAML + 命令行覆盖 tp
   sa_b = prepare_server_args(['--model-path','dummy','--config','/tmp/sgl.yaml','--tp-size','2'])
   print('B (yaml + cli tp=2)  port =', sa_b.port, 'tp =', sa_b.tp_size)
   "
   ```
3. **需要观察的现象**：场景 A 的 `tp` 应为 `4`（来自 YAML）；场景 B 的 `tp` 应为 `2`（命令行覆盖了 YAML）。
4. **预期结果**：A 输出 `tp = 4`，B 输出 `tp = 2`，证明命令行参数确实盖过配置文件。
5. 本实践可离线运行（dummy 模型不加载权重）；若 YAML 路径或字段名写错，合并器会抛 `ValueError`，这是预期行为。

#### 4.2.5 小练习与答案

**练习 1**：如果 YAML 里写了 `skip-tokenizer-init: true`，合并器会输出什么样的 argv 片段？

**答案**：因为 `skip_tokenizer_init` 是 `store_true` 开关，合并器会输出 `["--skip-tokenizer-init"]`（仅追加 flag），而不是 `["--skip-tokenizer-init", "true"]`（后者会让 argparse 报错）。规则见 [srt/server_args_config_parser.py:162-177](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args_config_parser.py#L162-L177)。

**练习 2**：为什么合并器要拒绝「非 store/store_true 的动作」出现在 YAML 里？

**答案**：见 [srt/server_args_config_parser.py:34-43](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args_config_parser.py#L34-L43) 与 [srt/server_args_config_parser.py:147-150](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args_config_parser.py#L147-L150)。这些动作（如自定义 `Action`）需要复杂的值转换，YAML 的简单「key: value」无法表达，强行支持会引入歧义，故直接报错暴露问题。

---

### 4.3 `__post_init__`：派生、校验与只读固化

#### 4.3.1 概念说明

数据类的 `__post_init__` 是构造完成后的「收尾钩子」。`ServerArgs.__post_init__` 是整个文件最复杂的方法，但它的**结构**却异常清晰——它被刻意写成一个**调度器（dispatcher）**：方法体本身几乎只做一件事，即按固定顺序调用一长串 `self._handle_*` 辅助方法。

为什么需要它？因为很多参数有依赖关系：

- `tp_size * pp_size` 必须能被节点数整除（校验）。
- `mem_fraction_static` 默认 `None`，要根据显存大小和 `chunked_prefill_size` 算出来（派生）。
- 某些注意力后端和某些量化方式不兼容（兼容性校验）。
- Mamba 模型不能用普通 RadixCache（模型专属调整）。

这些逻辑如果全塞进 `__post_init__` 会变成几千行的面条代码，所以被拆成几十个 `_handle_xxx`。

#### 4.3.2 核心流程

`__post_init__` 的执行可以抽象为三个阶段：

```text
阶段 1：早退判断
  └─ model_path ∈ {"none","dummy"}?  → 直接 return（供测试/离线用）

阶段 2：有序派生与校验（dispatcher）
  ├─ 模型源路径、多模态、SSL、ASR 校验
  ├─ 弃用参数、缺失默认值
  ├─ PD 分离、CUDA graph、各硬件后端（CPU/NPU/XPU/MPS/HPU）
  ├─ GPU 显存设置（推算 mem_fraction_static 等）
  ├─ 模型专属调整（关键：可能改大量字段）
  ├─ 注意力/采样/Mamba/Grammar 后端
  ├─ 数据/张量/流水线/上下文 并行
  ├─ MoE / EPLB / 投机解码
  └─ 其他校验

阶段 3：固化
  └─ materialize_declarations(self)  →  从此 ServerArgs 只读
```

**显存推算的数学**：`mem_fraction_static` 在用户不指定时由 `_handle_gpu_memory_settings` 推算。其定义（来自源码注释 [srt/server_args.py:4324-4333](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L4324-L4333)）为：

\[ \text{mem\_fraction\_static} = \frac{\text{GPU 显存总量} - \text{reserved\_mem}}{\text{GPU 显存总量}} \]

其中预留显存（ activations + cuda graph buffers）按经验式估算（单位 GB）：

\[ \text{reserved\_mem} = \text{chunked\_prefill\_size} \times 1.5 + \text{max\_bs} \times 2 \]

也就是说，`chunked_prefill_size` 越大，留给 KV 池的静态比例就越小——这是一个牵一发动全身的派生关系。

#### 4.3.3 源码精读

`__post_init__` 的开头，先看它的「调度器哲学」文档和早退分支：

[srt/server_args.py:3290-3323](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L3290-L3323) — 方法文档明确写了「Dispatcher style principles」五条原则（保持本方法是有序调度器、把细节塞进 helper、按依赖域排序而非历史顺序、隐藏厂商细节、每个 handler 只有一个清晰契约）；紧接着 `_validate_mamba_max_states_per_path()`，然后 `if self.model_path.lower() in ["none", "dummy"]: return`。

调度器中段，能看到典型的「特性钩子」被显式调用：

[srt/server_args.py:3446-3448](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L3446-L3448) — `from sglang.srt.arg_groups.speculative_hook import handle_speculative_decoding; handle_speculative_decoding(self)`。投机解码的所有派生/校验都被收进这个 hook，`__post_init__` 只负责在正确时机调用它。

**阶段 3 的固化点**——这是「只读」生效的分界线：

[srt/server_args.py:3491-3497](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L3491-L3497) — 注释说明：从此处起 `server_args` 携带的是**已解析配置**，任何进程的任何后续读者都直接读字段即可；调用 `materialize_declarations(self)` 把累积的「声明」一次性落到字段上（gate order，后写胜出）。

**只读是如何被强制执行的**——重写了 `__setattr__`：

[srt/server_args.py:7945-7962](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L7945-L7962) — 固化后（`_declarations_materialized=True`），任何在 `override()` 之外（`_in_override=False`）对非下划线字段的赋值，都会抛 `AttributeError`，并提示「请用 `get_context().override(...)`」。这把「运行时乱改配置」从「隐蔽 bug」提升为「立刻报错」。

**唯一的合法改写入口 `override()`**：

[srt/server_args.py:7911-7943](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L7911-L7943) — 它是固化后**唯一**的改写点：把白名单内（`resolvable=True`）字段记入声明栈（便于重新发布时复算），其余记入运行期变更日志，置 `_in_override=True` 后再 `setattr`，从而绕过上面的只读保护。这是「不可变优先」工程约定（见 `.claude/rules/general-code-style.md`）在配置层的落地。

> 设计要点：固化 + 只读 + 单一 `override()` 入口，三者合起来保证了——一旦服务启动完成，配置就是一份「洁净、可追溯、不可被随手改坏」的快照。这与 u3-l4 将要讲的 `RuntimeContext`「resolve-at-end」是一体两面。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到「固化后赋值会报错」与「`override()` 能合法改」的对比。
2. **操作步骤**：
   ```bash
   cd python/sglang
   python -c "
   from sglang.srt.server_args import ServerArgs
   sa = ServerArgs(model_path='dummy')
   print('固化标记 =', getattr(sa, '_declarations_materialized', '未设置(dummy 早退)'))
   # dummy 模型会早退, materialize 不会执行; 这里演示 override 的契约即可:
   sa.override('demo', tp_size=8)
   print('override 后 tp_size =', sa.tp_size)
   "
   ```
3. **需要观察的现象**：注意 dummy 模型走早退分支（[srt/server_args.py:3322-3323](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L3322-L3323)），`materialize_declarations` 未执行，`_declarations_materialized` 可能不存在；`override()` 仍能正常把 `tp_size` 改成 8。
4. **预期结果**：`override` 后 `tp_size = 8`。要真正触发「只读报错」需用真实模型走完 `__post_init__`，**待本地验证**：在真实启动的服务代码里 `server_args.tp_size = 99`，应看到 `AttributeError: server_args.tp_size assigned after resolution`。
5. 本实践为「源码阅读型」，重在理解 `override` 与 `__setattr__` 的协作，而非跑全量服务。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `__post_init__` 要在 `materialize_declarations` 之前先跑 `_handle_model_specific_adjustments`？

**答案**：模型专属调整（如某架构强制 `dtype="bfloat16"`）会产生大量「声明」，这些声明必须在固化**之前**全部收集好，再由 `materialize_declarations` 按 gate order 一次性落到字段。固化之后再改就没有合法途径了（除了 `override()`）。见 [srt/server_args.py:3486-3497](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L3486-L3497)。

**练习 2**：`override()` 为什么要区分 `declared`（白名单内）和 `rest`（白名单外）两份记录？

**答案**：白名单内（`resolvable=True`）的字段会被记入 `_resolved_overrides` 声明栈，重新发布配置时能复算出相同值（幂等）；白名单外的运行期调整只记日志（`_runtime_mutations`）用于审计，不参与复算。见 [srt/server_args.py:7923-7937](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L7923-L7937)。

---

### 4.4 arg_groups 分组钩子与覆盖注册表

#### 4.4.1 概念说明

`server_args.py` 已经 8800 多行了。如果每加一个特性（PD 分离、投机解码、HiSparse……）都往这个文件里堆代码，它会迅速变得不可维护。SGLang 的解法是 `srt/arg_groups/` 目录：**把按特性分组的解析/校验逻辑抽成独立模块**，`server_args.py` 只在 `__post_init__` 的合适时机「调用」它们。

这就是本讲标题里的「分组钩子（grouped hooks）」。它和 4.1 的「注解式声明」是两件事：注解式声明解决「字段怎么变成 CLI」，分组钩子解决「字段之间复杂的派生/校验/兼容性逻辑放哪」。

#### 4.4.2 核心流程

`arg_groups/` 目录目前包含几类成员：

| 文件 | 类型 | 作用 |
| --- | --- | --- |
| `arg_utils.py` | 基础设施 | `A`/`Arg`/`NS`/`add_cli_args_from_dataclass`（4.1 已讲） |
| `argparse_actions.py` | 基础设施 | 弃用参数的动作类 |
| `server_args_config_parser.py`（在 srt/ 根） | 基础设施 | YAML 合并（4.2 已讲） |
| `speculative_hook.py` | 特性钩子 | 投机解码参数的派生/校验 |
| `pd_disaggregation_hook.py` | 特性钩子 | PD 分离模式的参数规范化 |
| `hisparse_hook.py` | 特性钩子 | HiCache/HiSparse 校验 |
| `deepseek_v4_hook.py` | 特性钩子 | DeepSeek-V4 上下文并行校验 |
| `overrides.py` | 覆盖注册表 | 模型身份驱动的字段覆盖 |

钩子的调用模式很统一：`__post_init__` 里 `from sglang.srt.arg_groups.xxx_hook import handle_xxx; handle_xxx(self)`，把 `self`（即 `ServerArgs`）传进去，hook 内部读写它的字段。

#### 4.4.3 源码精读

**一个典型特性钩子——PD 分离**。它演示了「规范化 + 兼容性校验」两类工作：

[srt/arg_groups/pd_disaggregation_hook.py:15-58](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/pd_disaggregation_hook.py#L15-L58) — `handle_pd_disaggregation`：① 把 `mooncake_tcp` 这种「带传输后缀」的别名规范化成 `mooncake` 并设环境变量 `MC_FORCE_TCP`；② decode 节点根据开关强制 `disable_radix_cache` 的值；③ 校验 `--disaggregation-decode-enable-radix-cache` 与 hisparse/投机解码不兼容。整个 PD 分离的「参数语义」都收在这里，`server_args.py` 只有一行调用（[srt/server_args.py:3351](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L3351) `_handle_pd_disaggregation` 内部转发）。

**覆盖注册表 overrides.py——「声明式」而非「命令式」**。这是 `arg_groups` 里最大、最重要的模块（9 万字符），它的设计哲学与众不同：

[srt/arg_groups/overrides.py:14-28](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/overrides.py#L14-L28) — 文档说明：模型身份（`hf_config.architectures[0]`）驱动的配置调整在这里**声明**，在 `__post_init__` 末尾由 `materialize_declarations` 一次性落到字段；**模型代码绝不命令式地改 `ServerArgs`**。两种声明形式：`MODEL_OVERRIDES`（常量情况）和 `@register_model_override(arch)`（需要派生的情况，返回一个声明 dict）。

最简单的常量覆盖例子：

[srt/arg_groups/overrides.py:64-69](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/overrides.py#L64-L69) — `MistralLarge3ForCausalLM` 等架构无论用户请求什么 `dtype`，都强制 `bfloat16`。这种「按模型架构改默认」的逻辑，过去散落在 `server_args.py` 的大量 `if` 里，现在被集中到注册表。

**弃用动作——平滑迁移老 CLI**。当参数改名时，老名字不能立刻删（会破坏现有脚本），而是挂一个「弃用动作」，打印警告并转发到新字段：

[srt/arg_groups/argparse_actions.py:32-42](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/argparse_actions.py#L32-L42) — `DeprecatedAction`：老 flag 出现时只打印黄色警告，不存储任何值（用于「已彻底无操作」的参数）。

[srt/arg_groups/argparse_actions.py:44-67](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/argparse_actions.py#L44-L67) — `DeprecatedStoreTrueAction`：老 flag 仍生效（存 True），但提示用新 flag。`server_args.py` 的 `add_cli_args` 里就有一批这样的手工注册（如 `--stream-output` → `--incremental-streaming-output`，见 [srt/server_args.py:7635-7641](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L7635-L7641)）。

> 设计要点：`arg_groups/` 是「关注点分离」的范本——CLI 派生、YAML 合并、特性派生、模型覆盖、弃用迁移，各有其位。这让 8800 行的 `server_args.py` 没有继续膨胀到不可收拾。

#### 4.4.4 代码实践

1. **实践目标**：跟踪一个特性钩子的调用链，理解「`__post_init__` 调一行 → hook 干一堆活」的分工。
2. **操作步骤**：
   - 打开 [srt/server_args.py:3567-3573](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L3567-L3573)（`_handle_pd_disaggregation`），看它如何 `from ...pd_disaggregation_hook import handle_pd_disaggregation` 并调用。
   - 再打开 [srt/arg_groups/pd_disaggregation_hook.py:15-58](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/pd_disaggregation_hook.py#L15-L58)，逐行标注它做了哪几件事（规范化别名 / 设 disable_radix_cache / 兼容性校验 / 推算 extra_slots）。
3. **需要观察的现象**：`server_args.py` 侧只有「import + 调用」两行，真正的逻辑全在 hook 里。
4. **预期结果**：你能用一句话描述 PD 钩子的职责，并指出它若不抽成独立文件，这些代码原本会塞进 `__post_init__` 的哪个位置。
5. 这是纯源码阅读实践，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`overrides.py` 里为什么要求模型覆盖「声明式返回 dict」而不是「直接 `server_args.dtype = ...`」？

**答案**：声明式让所有覆盖在 `materialize_declarations` 处按统一的 gate order（后写胜出）一次性落地，顺序可控、可审计、可复算；若各处直接命令式赋值，覆盖顺序会取决于调用次序，难以推理，也无法在固化时统一应用。见 [srt/arg_groups/overrides.py:14-28](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/overrides.py#L14-L28)。

**练习 2**：`DeprecatedAliasStoreAction` 和 `DeprecatedAction` 有什么区别？

**答案**：`DeprecatedAction` 只打印警告、不存值（老参数已彻底无效）；`DeprecatedAliasStoreAction` 打印警告**并**把值存到新 `dest`（老参数是新旧名共存期，仍需生效）。见 [srt/arg_groups/argparse_actions.py:32-42](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/argparse_actions.py#L32-L42) 与 [srt/arg_groups/argparse_actions.py:98-110](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/argparse_actions.py#L98-L110)。

---

## 5. 综合实践

**任务**：给 `ServerArgs`「假装」新增一个参数，走完它从声明到生效的完整生命周期，把本讲四个模块串起来。

步骤：

1. **声明字段**（对应 4.1）：在 `srt/server_args.py` 的某个合适 section（比如 `# Model and tokenizer` 下方）加一行（**这是示例代码，仅用于学习，不要提交**）：
   ```python
   enable_foo: A[bool, "Turn on the foo optimization.", NS("schedule")] = False
   ```
2. **验证 CLI 自动派生**（对应 4.1 + 4.2）：
   ```bash
   cd python/sglang
   python -m sglang.srt.server_args --help 2>/dev/null || \
   python -c "
   import argparse
   from sglang.srt.server_args import ServerArgs
   p = argparse.ArgumentParser()
   ServerArgs.add_cli_args(p)
   ns = p.parse_args(['--enable-foo'])
   print('enable_foo parsed =', ns.enable_foo)
   "
   ```
   预期：不写 `add_argument`，`--enable-foo` 已自动可用，`enable_foo parsed = True`。
3. **验证配置合并**（对应 4.2）：写一个 YAML `foo.yaml` 含 `enable-foo: true`，用 `prepare_server_args(['--model-path','dummy','--config','foo.yaml'])` 构造，确认 `sa.enable_foo is True`。
4. **观察派生与固化**（对应 4.3）：在 `__post_init__` 的 dispatcher 里加一行 `self._handle_missing_default_values()` 之后的日志 `print("foo at post_init:", self.enable_foo)`，确认它在 `materialize_declarations` 之前已被读取、之后只读。
5. **（可选）真实启动验证**：用一个小模型 `sglang serve <model> --enable-foo` 启动，观察日志中是否出现你的参数。**待本地验证**（需要 GPU 与模型权重）。

完成后，你应当能复述：一行注解 → 自动 CLI → YAML 合并 → `__post_init__` 派生 → 固化只读。这就是 `ServerArgs` 配置体系的完整闭环。

> 注意：本实践会临时改动 `server_args.py`，仅用于本地学习，结束请用 `git checkout srt/server_args.py` 还原，不要把示例字段提交。

## 6. 本讲小结

- `ServerArgs` 是 SGLang 的**单一配置源**：一个 8800+ 行的 `@dataclass`，承载所有服务参数，被三大管理器进程共享读取。
- 参数采用**注解式声明**：`字段: A[类型, Arg(...), NS(...)] = 默认值`，由 `add_cli_args_from_dataclass` 自动派生成 argparse 选项，做到「字段即 CLI」。
- `prepare_server_args` 是解析入口，确立 **CLI > YAML Config > Defaults** 的优先级（靠 `ConfigArgumentMerger` 把 YAML 转成 argv 并排在命令行之前实现）。
- `__post_init__` 是一个**有序调度器**，把派生/校验逻辑拆成几十个 `_handle_*`，末尾 `materialize_declarations` 固化配置。
- 固化后 `ServerArgs` **只读**：`__setattr__` 会拒绝裸赋值，唯一合法改写入口是 `override()`，落实「不可变优先」约定。
- `arg_groups/` 目录用**分组钩子 + 声明式覆盖注册表**控制 `server_args.py` 的膨胀：特性逻辑（PD/投机/HiSparse）抽成 hook，模型专属调整集中到 `overrides.py`，弃用迁移交给 `argparse_actions.py`。

## 7. 下一步学习建议

- **紧接 u3-l4（RuntimeContext）**：本讲的 `NS("parallel")`/`NS("schedule")` 命名空间标记，正是 `RuntimeContext` 构建「上下文配置树」的输入。下一讲会讲清「resolve-at-end」「资源/stream/buffer 租约」与 `get_context().override()` 的关系——也就是本讲 `override()` 在运行时的真正落点。
- **回看 u3-l1/u3-l2**：带着本讲的认知重读启动链，你会看清 `prepare_server_args` 在 `launch_server` 里的位置，以及 `ServerArgs` 如何被序列化传给 Scheduler/Detokenizer 子进程。
- **后续 u4（调度核心）**：Scheduler 事件循环里几乎所有阈值（`max_running_requests`、`chunked_prefill_size`、`mem_fraction_static`）都来自本讲的 `ServerArgs`，届时可直接回查字段默认值与派生公式。
- **深入阅读建议**：通读 [srt/server_args.py:3290-3497](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L3290-L3497)（`__post_init__` 全貌）和 [srt/arg_groups/overrides.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/arg_groups/overrides.py) 的模型覆盖注册表，理解「声明式配置」如何让超大类保持可维护。
