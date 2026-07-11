# 注册一个新模型进入流水线

## 1. 本讲目标

本讲是单元 7 的首讲，回答一个最实际的二次开发问题：

> 「我已经看懂了整条降级链（u2–u5），也看懂了 TinyStories 怎么打通（u6）。现在我想把自己的模型接进来，到底要改哪些文件？」

学完后你应当能做到：

- 说出新增一个可降级模型的「**两步法**」：写 Python adapter、在 `nix/models.nix` 加一条 `registerModel` 注册。
- 解释 `registerModel` 的三个流水线开关 `allowHwExterns` / `fpPrimsSv` / `slangPerFileExternModules` 各自在哪一站被消费、注入什么环境变量、控制下游脚本的什么行为，并能判断新模型何时该打开它们。
- 看清 `flake.nix` 是如何把「Python 环境」「模型权重快照」「adapter 路径」三样东西绑成一个不联网、可复现的注册条目。
- 能照葫芦画瓢，写出把一个更小 HuggingFace 模型接入流水线的 nix 片段与 adapter 函数签名。

本讲是对前置讲义 [u3-l5](u3-l5-pipeline-nix-orchestration.md)（pipeline.nix 编排层）和 [u6-l1](u6-l1-selftest-wrapper-autogen.md)（自测外壳自动生成）的应用层收束：前两讲讲「机制」，本讲讲「怎么用这套机制加东西」。

## 2. 前置知识

阅读本讲前，你需要先建立以下几个概念（均来自前置讲义，这里只做一句话回顾）：

- **降级链（lowering chain）**：PyTorch → torch-MLIR → CIRCT → SystemVerilog → Yosys RTLIL，每一段叫一个「站」，由 `nix/pipeline.nix` 串成一条可缓存派生链（见 u1-l1、u3-l5）。
- **adapter 契约**：每个 PyTorch 模型通过一个 adapter 向流水线暴露，必须实现 `build_model(model_path)` 与 `example_inputs()`，可选 `EXPORT_STRICT`；契约的强制执行方是 `compile-pytorch.py`（见 u2-l1、u2-l2）。
- **`registerModel` / `mkPipeline`**：pipeline.nix 用 `let self = {...}; in self;` 的惰性自引用把 10 个阶段串起来；`registerModel` 把「torch 方言入口」+「开关」打包成 `{ pipeline; }`（见 u3-l5）。
- **流水线开关**：`allowHwExterns` / `fpPrimsSv` 服务于 SV 导出站（浮点 extern），`slangPerFileExternModules` 服务于 Yosys 前端站（大设计省内存），其工程背景见 u3-l4、u5-l1、u6-l3。
- **可复现快照**：模型权重用 Nix 的 `fetchurl` + `linkFarm` 钉成本地快照，整链不联网（见 u2-l1）。

如果以上某条你还很陌生，建议先回到对应讲义。本讲不再重复其细节，只承接其结论。

## 3. 本讲源码地图

本讲涉及的关键文件，按「数据流自上而下」排列：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `nix/models.nix` | **模型注册表**，全仓库唯一「新增模型要改」的地方 | 两个现有条目 `matmul` 与 `tiny-stories-1m-baseline-float` 的写法 |
| `nix/pipeline.nix` | 降级链编排库 | `registerModel`、`mkTorchInput`、`mkPipeline`，以及三个开关如何流进 `mkSvDerivation` / `mkIlDerivation` |
| `scripts/compile-pytorch.py` | torch 方言入口脚本，adapter 契约的强制执行方 | `--adapter` / `--model-path` / `--out` 三参，以及缺 `build_model`/`example_inputs` 时的报错 |
| `src/matmul_adapter.py` | 最小核 matmul 的 adapter | 无开关、无权重的最简 adapter 长什么样 |
| `TinyStories/model_adapter.py` | TinyStories-1M 的 adapter | 真实大模型 adapter：`EXPORT_STRICT=False`、`from_pretrained` |
| `flake.nix` | 顶层装配 | `pythonWithTorch` / `pythonWithTinyStories`、`tinyStories1m.snapshot`、`modelRegistry` 的接线 |

一句话定位：**`models.nix` 是入口，`pipeline.nix` 是引擎，`compile-pytorch.py` + adapter 是被引擎驱动的「第一站」**。加新模型只动入口和 adapter，引擎不用碰。

## 4. 核心概念与源码讲解

### 4.1 adapter + 注册两步法

#### 4.1.1 概念说明

LLM2FPGA 的设计把「**模型是什么**」和「**模型怎么进入流水线**」彻底分开：

- **模型是什么**：由一个 Python adapter 描述——怎么把权重/配置加载成一个 `torch.nn.Module`（`build_model`），以及用什么形状的输入当「模具」把动态图脱成静态图（`example_inputs`）。这部分是纯 Python，和 Nix 无关。
- **模型怎么进入流水线**：由 `nix/models.nix` 里的一条 `registerModel { ... }` 描述——用哪个 Python 环境跑、传什么命令行参数、打开哪些流水线开关。这部分是纯 Nix，和模型算法无关。

这种分离的好处是：**加一个新模型，不需要懂降级链的任何一站**。你只需写 Python（描述模型）和写一条 Nix（描述入口），降级链的 9 个 shell 脚本和 10 个派生会自动复用。

#### 4.1.2 核心流程

新增模型的两步法伪代码：

```
步骤 1（Python 侧）：写 adapter，例如 MyModel/model_adapter.py
    def build_model(model_path):        # 怎么造出 nn.Module
        ...
    def example_inputs():               # 用什么形状的输入脱图（必须与 forward 形参一一对应）
        ...
    EXPORT_STRICT = True/False          # 可选：torch.export 是否严格校验

步骤 2（Nix 侧）：在 nix/models.nix 加一条
    my-model = registerModel {
      name = "my-model";
      torchInputBuildInputs = [ <带 torch（/transformers）的 python env> ];
      torchInputCommand = ''
        export PYTHONPATH="<模型源码目录>:<torch-mlir 的 python 路径>:''${PYTHONPATH:-}"
        python ${compilePyTorch} \
          --adapter <MyModel/model_adapter.py> \
          [--model-path <权重快照>] \
          --out "$out"
      '';
      # 按需打开（见 4.2）：
      # allowHwExterns / fpPrimsSv / slangPerFileExternModules
    };
```

`registerModel` 内部做了什么（详见 4.1.3 源码）：

```
registerModel { name, torchInputCommand, torchInputBuildInputs, ...开关 }
   │
   ├─ mkTorchInput      # 把 torchInputCommand 包成一个 runCommand 派生
   │     └─ 产物：<name>-torch.mlir（torch 方言文本）
   │
   └─ mkPipeline { torch, ...开关 }
         └─ torch → linalg → cf → handshake → hs-ext → hw0 → hw → hw-clean → sv → il
   => 返回 { pipeline = { torch, linalg, cf, ..., sv, il }; }
```

之后在 `flake.nix` 里用 `modelRegistry."my-model".pipeline.<某站>` 取出你关心的阶段产物，再按需接综合/自测/资源报告。

#### 4.1.3 源码精读

**先看注册表 `nix/models.nix`**——这是新增模型唯一要改的文件。它整个文件就是一个「按文件路径接收一堆参数、返回一个 attrset」的函数：

[nix/models.nix:1-3](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L1-L3) —— 函数头，列出所有从 `flake.nix` 注入的依赖：`registerModel`（引擎）、两个 Python 环境、torch-mlir、模型快照、adapter 路径等。

其中最关键的两行注册示例：

[nix/models.nix:8-18](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L8-L18) —— **matmul 注册条目**。注意三个要点：① 没有任何开关（`allowHwExterns` 等都走默认 `false`/`null`），因为它只是 int32 点积、没有浮点 extern，设计也极小；② `torchInputBuildInputs = [ pythonWithTorch ]`，只需最小 Python 环境；③ `torchInputCommand` 设置好 `PYTHONPATH`（matmul 源码目录 + 仿真目录 + torch-mlir 的 Python 绑定路径），再调 `compile-pytorch.py`，把 torch 方言 MLIR 写到 Nix 自动注入的 `$out`。

[nix/models.nix:20-33](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L20-L33) —— **TinyStories 注册条目**，是你要模仿的「真实大模型」模板。对比 matmul，它多了三样：① `allowHwExterns = true; slangPerFileExternModules = true; inherit fpPrimsSv;`（三个开关全开，原因见 4.2）；② `torchInputBuildInputs = [ pythonWithTinyStories ]`（需要 `transformers` 库）；③ 命令行多了 `--model-path ${tinyStories1m.snapshot}`（权重快照）。注意 attr 名 `"tiny-stories-1m-baseline-float"` 带连字符所以必须加引号，且与 `name = ...` 保持一致——前者是 `modelRegistry` 的索引键，后者是各派生 store 路径的前缀。

[nix/models.nix:5-6](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L5-L6) —— `torchMlirPythonPath`。这是把 torch-mlir 的 Python 绑定（`torch_mlir.fx`）加进 `PYTHONPATH` 的固定拼装，两个现有条目都用到它。新模型条目照抄即可。

**再看引擎侧 `nix/pipeline.nix` 的 `registerModel`**，看清你写的 `{ ... }` 是怎么变成一条链的：

[nix/pipeline.nix:143-154](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L143-L154) —— `registerModel` 函数体。它先调 `mkTorchInput` 把你的命令归一成一个 torch 派生，再调 `mkPipeline` 把这个 torch 派生连同开关一起喂进降级链，返回 `{ pipeline; }`。注意它的参数列表：`torchMlirInput ? null` 与 `torchInputCommand ? null` 二选一（见 4.1.4 小练习），三个开关都是带默认值的可选参数。

[nix/pipeline.nix:15-28](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L15-L28) —— `mkTorchInput` 的三分支调度：① 给了 `torchMlirInput`（现成的 torch 方言 MLIR 文件）就直接用；② 给了 `torchInputCommand` 就用 `runCommand` 包成派生，命令在 `set -euo pipefail` 下跑；③ 都不给就 `throw` 一个明确的中文友好错误。matmul 与 TinyStories 都走第②条。

[nix/pipeline.nix:97-141](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L97-L141) —— `mkPipeline` 用 `let self = {...}; in self;` 的惰性自引用串起 10 个阶段（这是 u3-l5 的核心，本讲不重复）。你只需知道：取 `.pipeline.il` 不会触发 `.pipeline.sv` 之前的全量重算——这正是「加新模型只改入口、不改引擎」能成立的底层保证。

**最后看 adapter 契约的强制执行方 `scripts/compile-pytorch.py`**——它定义了「adapter 必须长什么样」：

[scripts/compile-pytorch.py:41-46](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L41-L46) —— 契约检查：用 `getattr` 取 adapter 的 `build_model` 与 `example_inputs`，缺任一个就 `SystemExit` 报错并明确提示「must define build_model(model_path) and example_inputs()」。**理解 adapter 契约要看消费方（这里），而不是 adapter 自己**——这是 u2-l1 已确立的铁律。

[scripts/compile-pytorch.py:48-57](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L48-L57) —— 调用契约：`build_model(args.model_path).eval()` 造模型（`args.model_path` 来自 `--model-path`，没有就是 `None`），`torch.export.export(..., strict=EXPORT_STRICT)` 脱图，`export_and_import(..., output_type="torch")` 翻译成 MLIR，最后 `args.out.write_text(...)` 落盘并 `print`。

对照两个现成 adapter，可以看出「最小」与「真实」的差异：

[src/matmul_adapter.py:11-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L11-L19) —— matmul adapter：`build_model` 忽略 `model_path`（无权重），`example_inputs()` 返回两个 `int32[16]`，无 `EXPORT_STRICT`（走默认 `True`）。

[TinyStories/model_adapter.py:9-24](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py#L9-L24) —— TinyStories adapter：`EXPORT_STRICT = False`（放宽严格校验，否则 HuggingFace 模型脱不出图），`build_model` 要求 `model_path` 非空并 `from_pretrained`，`example_inputs()` 返回一个 `int64[1,1]` 的 token id。

#### 4.1.4 代码实践

**实践目标**：用「读源码 + 预测报错」的方式，验证你对两步法与 adapter 契约的理解，不实际运行。

**操作步骤**：

1. 打开 [nix/models.nix:8-18](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L8-L18) 与 [nix/models.nix:20-33](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L20-L33)，把两个条目的差异填进下表（只看结构，不看开关含义）：

   | 维度 | matmul | tiny-stories-1m-baseline-float |
   | --- | --- | --- |
   | Python 环境 | `pythonWithTorch` | ？ |
   | `--model-path` | 无 | ？ |
   | 开关 | 全默认 | ？ |
   | `EXPORT_STRICT` | ？（看 adapter） | ？（看 adapter） |

2. 打开 [scripts/compile-pytorch.py:41-46](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L41-L46)，假设你写的新 adapter 只定义了 `build_model`、忘了 `example_inputs`，**指出具体哪一行会触发、报什么错**。

3. 再看 [scripts/compile-pytorch.py:34-37](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L34-L37)，确认 `--model-path` 是可选参数（`parser.add_argument("--model-path")`，无 `required=True`）。预测：若 adapter 像 TinyStories 一样在 `build_model` 里检查 `model_path is None` 抛错，但调用方忘了传 `--model-path`，错误会从哪一层冒出来。

**需要观察的现象 / 预期结果**：

- 步骤 1 表格答案：Python 环境 = `pythonWithTinyStories`；`--model-path` = `${tinyStories1m.snapshot}`；开关 = 三个全开；`EXPORT_STRICT`：matmul 走默认 `True`、TinyStories 显式 `False`。
- 步骤 2：[compile-pytorch.py:43-46](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L43-L46) 的 `if build_model is None or example_inputs is None:` 为真，`raise SystemExit(f"{args.adapter} must define build_model(model_path) and example_inputs()")`。
- 步骤 3：错误从 **adapter 层**（`build_model` 内的 `raise RuntimeError(...)`）冒出来，而不是 `compile-pytorch.py`——因为 `--model-path` 缺省为 `None`，`compile-pytorch.py` 把 `None` 传给 `build_model`，由 adapter 自己决定是否拒绝。这正是「契约由消费方定义、但参数校验可由 adapter 自定」的分工。

> 本实践为源码阅读型，无需运行命令；若要在本地实证步骤 2 的报错，可在 `nix develop` 里直接 `python scripts/compile-pytorch.py --adapter <残缺 adapter> --out /tmp/x.mlir`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `nix/models.nix` 里 matmul 用 `matmul = registerModel {...}`（不加分号引号），而 TinyStories 用 `"tiny-stories-1m-baseline-float" = registerModel {...}`（加引号）？

**答案**：Nix 的 attr 名若只含字母/数字/下划线/连字符可直接写，但**含连字符的属性名在 Nix 中必须用字符串引号包裹**（否则会被解析成减法）。`matmul` 是合法裸标识符；`tiny-stories-1m-baseline-float` 含连字符，必须加引号。两者内部都还显式写了 `name = "..."`，`name` 用于派生 store 路径前缀，attr 名用于在 `modelRegistry` 里索引。

**练习 2**：`registerModel` 同时接受 `torchMlirInput` 和 `torchInputCommand`。如果你手上已经有一份**现成的** torch 方言 MLIR 文件（比如别人给你导好的），你会用哪一个？为什么？

**答案**：用 `torchMlirInput = <那份 mlir 文件的 nix 路径>`。`mkTorchInput` 会优先识别它、直接当作 torch 派生，跳过 `runCommand` 和 Python 环境（见 [pipeline.nix:17-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L17-L19)）。这适合「我想复用别人的前端产物、只重跑后端降级」的调试场景，省去装 torch / transformers 的开销。

**练习 3**：matmul adapter 的 `example_inputs()` 返回两个 `int32[16]`，而 `MatmulModule.forward(self, a, b)` 接收两个参数。如果把 `example_inputs()` 改成只返回一个张量，会发生什么？

**答案**：`torch.export.export(model, tuple(example_inputs()), ...)` 会因为输入数量（1）与 `forward` 形参数量（2）不匹配而报错——torch.export 用 `example_inputs` 当「模具」，**数量、形状、dtype 必须与 forward 形参一一对应**（这是 u2-l1 的铁律）。错误在 [compile-pytorch.py:49-53](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L49-L53) 的 `torch.export.export(...)` 调用处抛出，不会等到 MLIR 翻译阶段。

---

### 4.2 registerModel 选项开关

#### 4.2.1 概念说明

`registerModel` 有三个布尔/路径开关：`allowHwExterns`、`fpPrimsSv`、`slangPerFileExternModules`。它们的存在说明一个事实：**不同模型在降级链的不同站会遇到不同性质的麻烦**，而 `models.nix` 这个入口需要一种「不动引擎脚本、只拨开关」的方式来表达这些差异。

三个开关的共同特点：

- 它们都不改变降级链的**结构**（还是那 10 站），只改变某一站脚本的**行为**（通过环境变量）。
- 它们都用 Nix 的 `optionalString` 注入环境变量，因此**开关状态会成为派生 hash 的一部分**——同一模型不同开关配置得到不同 store 路径，互不污染缓存（这是 u3-l5 缓存隔离机制的直接体现）。
- matmul 全关（设计小、无浮点），TinyStories 全开（设计大、有浮点）——这两个极端恰好定义了开关的「何时需要打开」。

#### 4.2.2 核心流程

三个开关的对照表（这是本模块的核心，建议记下来）：

| 开关（`registerModel` 参数） | 默认值 | 在 `pipeline.nix` 哪里消费 | 注入的环境变量 | 控制下游脚本的什么行为 | 何时需要打开 |
| --- | --- | --- | --- | --- | --- |
| `allowHwExterns` | `false` | `mkSvDerivation`（SV 导出站） | `ALLOW_HW_EXTERNS=1` | `hw_clean_to_sv.sh` 放行 `hw.module.extern` 黑盒，不再 fail-fast 拒绝 | 模型含浮点算子，被 CIRCT 补丁 0015 降为 extern 时 |
| `fpPrimsSv` | `null` | `mkSvDerivation`（SV 导出站） | `FP_PRIMS_SV=<路径>` | `hw_clean_to_sv.sh` 用该 SV 文件给所有浮点 extern 提供可综合实现 | 同上——必须与 `allowHwExterns` 一起开，且文件要覆盖全部 extern |
| `slangPerFileExternModules` | `false` | `mkIlDerivation`（Yosys 前端站） | `YOSYS_SLANG_PER_FILE_EXTERNS=1` | `sv_to_il.sh` 走「逐文件 extern」模式，按文件读入、把找不到定义的模块当 blackbox，压低峰值内存 | 设计大到单次 elaborate 会 OOM 时 |

一个简单的判断口诀：

- **设计里有浮点？** → 开 `allowHwExterns` + `fpPrimsSv`（两者捆绑，缺一不可：前者允许 extern 存在，后者提供 extern 的实现）。
- **设计大到一个文件塞不下、slang 单次展开 OOM？** → 开 `slangPerFileExternModules`。

matmul 两个都「否」（int32 点积、几行代码）→ 全关；TinyStories 两个都「是」（浮点 LLM、约 141 倍超配）→ 全开。

#### 4.2.3 源码精读

开关在引擎侧的落点，集中在 `pipeline.nix` 的两个 `mk*Derivation`：

[nix/pipeline.nix:74-84](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L74-L84) —— **`mkSvDerivation` 消费 `allowHwExterns` 与 `fpPrimsSv`**。看这两行：
- 第 76-78 行：`optionalString allowHwExterns '' export ALLOW_HW_EXTERNS=1 ''`——只有开关为真时才 export 这个环境变量。下游 `hw_clean_to_sv.sh` 默认会 grep 出所有 `hw.module.extern` 并 `exit 1` 拒绝；只有看到 `ALLOW_HW_EXTERNS=1` 才放行（这是 u3-l4 讲过的「禁止裸 extern」安全门）。
- 第 79-81 行：`optionalString (fpPrimsSv != null) '' export FP_PRIMS_SV=${fpPrimsSv} ''`——把浮点原语 SV 文件路径暴露给下游，下游会把它拷成 `zz_circt_fp_primitives.sv` 追加进 `sources.f`，为每个浮点 extern 提供实现（u6-l3）。

[nix/pipeline.nix:86-95](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L86-L95) —— **`mkIlDerivation` 消费 `slangPerFileExternModules`**。第 88-90 行：`optionalString slangPerFileExternModules '' export YOSYS_SLANG_PER_FILE_EXTERNS=1 ''`。下游 `sv_to_il.sh` 据此切换 `read_slang` 的两种模式：默认单次 elaborate（一条命令带 `--top main`，适合小设计），或逐文件 extern（每文件一条 `--extern-modules`，把找不到定义的模块当 blackbox 占位，压低峰值内存——这是 u5-l1 讲过的两种模式）。

[nix/pipeline.nix:130-139](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L130-L139) —— 看 `mkPipeline` 内部如何把开关**逐站传递**：`sv = mkSvDerivation { inherit allowHwExterns fpPrimsSv; ... }`、`il = mkIlDerivation { inherit slangPerFileExternModules; ... }`。开关只在需要的那一站被消费，其它站完全无感——这就是「开关不改变链结构、只改变单站行为」的实现。

**两个开关的捆绑关系**很关键。回看 [nix/models.nix:22-24](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L22-L24)：TinyStories 同时设了 `allowHwExterns = true;` 和 `inherit fpPrimsSv;`（`fpPrimsSv` 是从 `flake.nix` 传进来的 `./rtl/fp/circt_fp_primitives.sv`，见 [flake.nix:199](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L199)）。只开 `allowHwExterns` 不给 `fpPrimsSv`，下游会发现「extern 被允许了但没有实现」而报错；只给 `fpPrimsSv` 不开 `allowHwExterns`，则 extern 在第一道门就被拒。所以**这两个开关要么都不动（无浮点），要么一起开（有浮点）**。

#### 4.2.4 代码实践

**实践目标**：建立「开关 → 环境变量 → 下游脚本行为 → 报错路径」的完整映射，并预测错误配置的后果。

**操作步骤**：

1. 在 [pipeline.nix:74-95](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L74-L95) 里，为每个开关画一条「流向箭头」：
   `registerModel 的 X → mk*Derivation 的 optionalString → export 环境变量 Y → 下游脚本 Z 的某段逻辑`。
2. 打开 `scripts/pipeline/hw_clean_to_sv.sh`，找到读取 `ALLOW_HW_EXTERNS` 与 `FP_PRIMS_SV` 的那段逻辑，确认它如何 grep `hw.module.extern`、何时 `exit 1`、何时放行并拷贝 `FP_PRIMS_SV`。
3. 打开 `scripts/pipeline/sv_to_il.sh`，找到读取 `YOSYS_SLANG_PER_FILE_EXTERNS` 的分支，确认两种 `read_slang` 模式的区别。

**需要观察的现象 / 预期结果**：

- 三个流向箭头：
  - `allowHwExterns → mkSvDerivation → ALLOW_HW_EXTERNS=1 → hw_clean_to_sv.sh 放行 extern`
  - `fpPrimsSv → mkSvDerivation → FP_PRIMS_SV=<path> → hw_clean_to_sv.sh 拷贝该 SV 补 extern 实现`
  - `slangPerFileExternModules → mkIlDerivation → YOSYS_SLANG_PER_FILE_EXTERNS=1 → sv_to_il.sh 走逐文件 extern`
- 预测错误配置：
  - **TinyStories 只开 `allowHwExterns`、不给 `fpPrimsSv`**：extern 被允许存在，但 `hw_clean_to_sv.sh` 发现某个 extern 在 `FP_PRIMS_SV` 里找不到实现 → 报错（缺实现）。
  - **TinyStories 关掉 `allowHwExterns`**：`hw_clean_to_sv.sh` grep 到 `hw.module.extern` 直接 `exit 1`（禁止裸 extern）。
  - **TinyStories 关掉 `slangPerFileExternModules`**：`sv_to_il.sh` 走单次 elaborate，巨大的设计撑爆内存 → Yosys 退出码 137/9（OOM，u5-l1 讲过）。

> 本实践为源码阅读型。若本地实证，可在 `nix/models.nix` 复制一份 TinyStories 条目、故意改错开关，`nix build .#...` 观察报错站点——但**不要提交这个改动**（本讲禁止改源码）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `allowHwExterns` 和 `fpPrimsSv` 通常成对出现？只开一个会怎样？

**答案**：`allowHwExterns` 只是「允许 extern 黑盒存在于输出」，但黑盒本身没有实现，Yosys 综合时仍是悬空符号；`fpPrimsSv` 才是「给这些黑盒提供可综合 SV 实现」。只开 `allowHwExterns`：SV 能导出但 extern 无实现，下游综合报未定义符号；只给 `fpPrimsSv`：extern 在 `hw_clean_to_sv.sh` 第一道 grep 就因 `ALLOW_HW_EXTERNS` 未设而 `exit 1`，根本走不到挂接实现那步。所以有浮点就必须成对开（见 4.2.3）。

**练习 2**：matmul 注册条目（[models.nix:8-18](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L8-L18)）完全没写这三个开关。请问它最终走到 `mkSvDerivation` 和 `mkIlDerivation` 时，`optionalString` 会做什么？

**答案**：三个开关都是带默认值的可选参数（`? false` / `? null`）。matmul 没传，于是 `allowHwExterns=false`、`fpPrimsSv=null`、`slangPerFileExternModules=false`。`optionalString false ...` 与 `optionalString (null != null) ...` 都求值为空字符串，于是 `ALLOW_HW_EXTERNS` / `FP_PRIMS_SV` / `YOSYS_SLANG_PER_FILE_EXTERNS` 三个环境变量都不 export，下游脚本走最严格的默认路径——这与 matmul 「无浮点、设计小」的事实匹配。

**练习 3**：假设你接进来一个新模型，它是纯整数运算（如量化后的 int8 模型）但**非常大**（比 TinyStories 还大）。你应该怎么设这三个开关？

**答案**：纯整数 → 没有浮点 extern → `allowHwExterns=false`、`fpPrimsSv=null`（与 matmul 同）；但设计非常大 → 单次 elaborate 会 OOM → `slangPerFileExternModules=true`（与 TinyStories 同）。这正是开关「按问题维度独立设置」的价值：浮点问题与规模问题互不相关，可任意组合。

---

### 4.3 Python 环境与模型快照绑定

#### 4.3.1 概念说明

两步法的第②步（`registerModel` 注册条目）里，有三样东西看起来「只是配置」、但其实是可复现性的命门：

1. **Python 环境**：模型要 `import torch`，HuggingFace 模型还要 `import transformers`。这些库必须在「跑 adapter 的那个 `runCommand`」里可用。
2. **torch-mlir 的 Python 绑定**：`compile-pytorch.py` 里有 `from torch_mlir.fx import export_and_import`，这个 `torch_mlir` 包来自 Nix 构建的 torch-mlir 派生，必须拼进 `PYTHONPATH`。
3. **模型权重快照**：真实 LLM 的权重不在仓库里（太大），需要从 HuggingFace 拉取并钉死版本，且整链不能联网。

这三样东西的绑定，是「写一条新注册条目」时最容易出错的地方。本模块讲清它们各自从哪来、怎么传到 adapter。

#### 4.3.2 核心流程

```
flake.nix 里准备三样东西：
  ① pythonWithTorch      = python + torch + packaging           （给 matmul）
     pythonWithTinyStories = python + torch + packaging + transformers （给 TinyStories）
  ② torchMlir            = nix 构建的 torch-mlir 派生（含 Python 绑定 torch_mlir.fx）
  ③ tinyStories1m.snapshot = linkFarm[ config.json, pytorch_model.bin ]（fetchurl 钉死 sha256）
                 │
                 ▼ 全部作为参数注入
  nix/models.nix:
     torchInputBuildInputs = [ pythonWithTinyStories ]          ← ①
     torchInputCommand 里：
       export PYTHONPATH="...:${torchMlirPythonPath}:..."       ← ②（torch_mlir.fx 在此）
       python compile-pytorch.py --model-path ${...snapshot}    ← ③
                 │
                 ▼ runCommand 执行
  scripts/compile-pytorch.py:
     model = adapter.build_model(args.model_path)               ← args.model_path = ③ snapshot
                 │
                 ▼
  TinyStories/model_adapter.py:
     AutoModelForCausalLM.from_pretrained(model_path, local_files_only=True)
     # 从 snapshot 目录读 config.json + pytorch_model.bin，绝不联网
```

关键点：**`local_files_only=True` 是快照机制的「宪法」**——它禁止 `from_pretrained` 回退到网络，强制只读本地目录。这让整条链可复现、可离线构建。

#### 4.3.3 源码精读

**Python 环境的定义在 `flake.nix`**：

[flake.nix:138-140](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L138-L140) —— 两个 Python 环境的对比：`pythonWithTorch` 只带 `torch` + `packaging`（matmul 够用）；`pythonWithTinyStories` 在此基础上加 `transformers`（TinyStories 的 `AutoModelForCausalLM` 需要）。两者都基于同一个 `python = pkgsLlvm21.python311`（u1-l3 讲过为何 torch-mlir 要用单独 pin 的 LLVM 包集）。新模型需要什么库，就照这两个的样子 `python.withPackages (ps: [ ... ])` 自建一个。

**模型权重快照的定义也在 `flake.nix`**：

[flake.nix:200-225](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L200-L225) —— `tinyStories1m` 的构造，这是「权重快照」的范本，逐段看：
- 第 201-202 行：`modelId = "roneneldan/TinyStories-1M"` 与 `revision = "77f1b168..."`——把模型 id 和 git commit 钉死。
- 第 203-208 行：`fetch = file: hash: pkgs.fetchurl { url = ".../resolve/${revision}/${file}"; inherit hash; }`——一个辅助函数，按文件名 + sha256 从 HuggingFace 拉。每个文件单独一个 `fetchurl`，单独一个 hash。
- 第 209-220 行：`snapshot = pkgs.linkFarm "tinystories-1m-hf-snapshot" [ ... ]`——`linkFarm` 把若干 `fetchurl` 产物组装成一个**看起来像 HuggingFace 模型目录**的派生（里面有 `config.json`、`pytorch_model.bin`）。这正是 `from_pretrained` 期望的目录结构。
- 第 221-225 行：返回 `{ snapshot; sourceDir; adapterPy; }`——`snapshot` 是权重，`sourceDir` 是 adapter 所在目录，`adapterPy` 是 adapter 文件路径。

**三样东西如何被注入 `models.nix`**：

[flake.nix:232-242](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L232-L242) —— `modelRegistry` 的 `import ./nix/models.nix { ... }` 调用，把上述三样东西（`pythonWithTorch` / `pythonWithTinyStories`、`torchMlir`、`tinyStories1m`、`fpPrimsSv`、各 adapter 路径）作为参数传进 `models.nix`。这一步是 flake 与 models.nix 的「接线板」。

[nix/models.nix:25-32](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L25-L32) —— 三样东西在 TinyStories 条目里的落点：① `[ pythonWithTinyStories ]` 作为 `torchInputBuildInputs`（Python 环境进入 `runCommand` 的 PATH）；② `PYTHONPATH` 里拼了 `${tinyStories1m.sourceDir}` 与 `${torchMlirPythonPath}`（后者由 [models.nix:5-6](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L5-L6) 拼好，让 `import torch_mlir.fx` 可解析）；③ `--model-path ${tinyStories1m.snapshot}`（权重快照传给 compile-pytorch.py）。

[TinyStories/model_adapter.py:15-20](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py#L15-L20) —— 快照最终在这里被消费：`AutoModelForCausalLM.from_pretrained(model_path, use_cache=False, attn_implementation="eager", local_files_only=True)`。`model_path` 就是上一步的 `snapshot` 目录，`local_files_only=True` 确保只从这个本地目录读，不联网。

**对比 matmul 的极简绑定**：[nix/models.nix:10-17](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L10-L17) 没有 `--model-path`（无权重），`PYTHONPATH` 里多了 `${matmulSrcDir}` 与 `${simDir}`（因为 matmul adapter 要 `from sim_utils import load_matmul_module`，见 [src/matmul_adapter.py:5](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L5)）。这印证了 `PYTHONPATH` 的拼法**因 adapter 的 import 习惯而异**——新模型的 adapter 若 import 了兄弟模块，就要把对应目录加进 `PYTHONPATH`。

#### 4.3.4 代码实践

**实践目标**：跟踪权重快照从 `flake.nix` 一路流到 adapter 的 `from_pretrained`，验证「整链不联网」的工程保证。

**操作步骤**：

1. 在 [flake.nix:200-225](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L200-L225) 里，列出构成 `tinyStories1m.snapshot` 的两个文件，写出各自的来源 URL 模板与 sha256 角色。
2. 跟踪 `snapshot` 的传递链：`flake.nix` 的 `tinyStories1m.snapshot` → [flake.nix:235](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L235) 注入 `models.nix` → [nix/models.nix:30](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L30) 作为 `--model-path` → [compile-pytorch.py:36](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L36) 接收为 `args.model_path` → [compile-pytorch.py:48](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L48) 传给 `build_model` → [TinyStories/model_adapter.py:15](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py#L15) 喂给 `from_pretrained`。
3. 解释 `local_files_only=True` 与 `linkFarm` 快照如何配合，保证构建机断网也能跑通。

**需要观察的现象 / 预期结果**：

- 步骤 1：两个文件是 `config.json`（sha256 `...67Wr...`，见 [flake.nix:212-213](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L212-L213)）与 `pytorch_model.bin`（sha256 `...B/lg...`，见 [flake.nix:216-218](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L216-L218)），URL 模板都是 `https://huggingface.co/roneneldan/TinyStories-1M/resolve/<revision>/<file>`。sha256 让 `fetchurl` 在内容不变时直接命中 Nix 缓存、不再触网。
- 步骤 3：`linkFarm` 把两个 `fetchurl` 产物拼成一个目录派生，其结构恰是 `from_pretrained` 期望的「含 `config.json` + 权重文件」的模型目录；`local_files_only=True` 则禁止 `from_pretrained` 联网校验/补下载。两者结合，权重的获取（`fetchurl`，构建期已固化）与使用（`from_pretrained`，只读本地）都被钉死，构建机无需 HuggingFace 在线。

> 本实践为源码阅读型。若本地实证，可在 `nix develop` 里 `echo $NIX_PATH` 后手动 `python -c "from transformers import AutoModelForCausalLM; AutoModelForCausalLM.from_pretrained('$(nix path-info .#tiny-stories-1m-baseline-float-selftest-all-memory-utilization)/..', local_files_only=True)"`——但更简单的实证是直接 `nix build .#tiny-stories-1m-baseline-float-selftest-all-memory-utilization` 并断网观察它仍能命中缓存。

#### 4.3.5 小练习与答案

**练习 1**：如果我接进来一个新的 HuggingFace 模型，权重快照要怎么做？

**答案**：照 [flake.nix:200-225](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L200-L225) 的模板，新写一个 `let` 块：① 设 `modelId` 与 `revision`（钉死 HF 仓库与 commit）；② 用 `fetch = file: hash: pkgs.fetchurl {...}` 辅助函数拉该模型必需的文件（至少 `config.json` 与权重文件，权重可能是 `pytorch_model.bin` 或 `model.safetensors`，看仓库）；③ 用 `linkFarm` 组装成目录派生；④ 返回 `{ snapshot; sourceDir; adapterPy; }`。sha256 第一次先用占位符、让 `fetchurl` 报错把正确 hash 告诉你（Nix 常用技巧）。

**练习 2**：为什么 `compile-pytorch.py` 的 `--model-path` 设计成**可选**参数（无 `required=True`），而不是强制要求？

**答案**：因为 matmul 这类无权重模型不需要它——`build_model` 会忽略 `model_path`（见 [matmul_adapter.py:11](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L11) 的 `_model_path` 下划线前缀）。把 `--model-path` 设为可选，让同一套 `compile-pytorch.py` 既能服务无权重核（matmul），也能服务有权重大模型（TinyStories）；「是否需要权重」由 adapter 自己在 `build_model` 里决定（TinyStories 用 `if model_path is None: raise` 自校验）。这是「入口通用、模型自管」的设计。

**练习 3**：TinyStories 条目里 `PYTHONPATH` 拼了 `${tinyStories1m.sourceDir}`。如果新 adapter 里写了 `from my_utils import foo`（`my_utils.py` 与 adapter 同目录），需要做什么？

**答案**：把该目录加进 `PYTHONPATH`——这与 TinyStories 拼 `sourceDir`、matmul 拼 `matmulSrcDir` + `simDir` 是同一个道理（adapter 的兄弟 import 靠 `PYTHONPATH` 解析）。注意 `compile-pytorch.py` 的 `load_adapter` 只会临时把 `path.parent`（adapter 所在目录）加进 `sys.path`（见 [compile-pytorch.py:15](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L15)），所以 adapter 本身能 import 同目录模块；但如果 `my_utils` 又 import 了别处的模块，仍需在 Nix 侧把那个目录拼进 `PYTHONPATH`。

---

## 5. 综合实践

**任务**：参照 `tiny-stories-1m-baseline-float` 的注册方式，为一个**更小**的 HuggingFace 因果语言模型（例如 `roneneldan/TinyStories-33M`，或任意一个小到能塞进 FPGA 预算的小模型）写一份接入流水线的「设计稿」——只需 nix 片段与 adapter 函数签名，不要求实际跑通。

**目标**：把本讲三个最小模块（两步法、开关、环境与快照绑定）一次性串起来用。

**交付物 1：adapter 函数签名**（新建 `MyModel/model_adapter.py`）

```python
# 示例代码（非项目原有文件，仅为综合实践示意）
from __future__ import annotations
import torch
from transformers import AutoModelForCausalLM

EXPORT_STRICT = False  # HuggingFace 模型通常需要放宽严格校验

def build_model(model_path: str | None) -> torch.nn.Module:
    if model_path is None:
        raise RuntimeError("MyModel adapter requires --model-path")
    return AutoModelForCausalLM.from_pretrained(
        model_path,
        use_cache=False,
        attn_implementation="eager",
        local_files_only=True,
    ).eval()

def example_inputs() -> tuple[torch.Tensor, ...]:
    # 形状必须与 forward 的形参（input_ids）一一对应
    return (torch.zeros((1, 1), dtype=torch.long),)
```

**交付物 2：flake.nix 里的权重快照**（照 [flake.nix:200-225](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L200-L225) 模板）

```nix
# 示例代码（非项目原有文件，仅为综合实践示意）
myModel = let
  modelId = "roneneldan/TinyStories-33M";       # 待确认：换成你选的模型
  revision = "<填该模型某 commit sha>";          # 待确认
  fetch = file: hash:
    pkgs.fetchurl {
      url = "https://huggingface.co/${modelId}/resolve/${revision}/${file}";
      inherit hash;
    };
  snapshot = pkgs.linkFarm "mymodel-hf-snapshot" [
    { name = "config.json";       path = fetch "config.json"       "<sha256-待确认>"; }
    { name = "pytorch_model.bin"; path = fetch "pytorch_model.bin" "<sha256-待确认>"; }
  ];
in {
  inherit snapshot;
  sourceDir = ./MyModel;
  adapterPy = ./MyModel/model_adapter.py;
};
```

**交付物 3：`nix/models.nix` 里的注册条目**

```nix
# 示例代码（非项目原有文件，仅为综合实践示意）
"my-model-baseline-float" = registerModel {
  name = "my-model-baseline-float";
  allowHwExterns = true;                # 有浮点 → 开
  slangPerFileExternModules = true;     # 设计可能较大 → 开（若极小可关）
  inherit fpPrimsSv;                    # 有浮点 → 必须与 allowHwExterns 成对
  torchInputBuildInputs = [ pythonWithTinyStories ];  # 需要 transformers
  torchInputCommand = ''
    export PYTHONPATH="${myModel.sourceDir}:${torchMlirPythonPath}:''${PYTHONPATH:-}"
    python ${compilePyTorch} \
      --adapter ${myModel.adapterPy} \
      --model-path ${myModel.snapshot} \
      --out "$out" >/dev/null
  '';
};
```

**说明：何时打开 `allowHwExterns` 与 `fpPrimsSv`**

- 只要新模型是**浮点**模型（绝大多数 HuggingFace LLM 都是），降级链走到 SV 导出站时，CIRCT 补丁 0015 会把浮点算子降级为 `hw.module.extern`，于是 **`allowHwExterns` 与 `fpPrimsSv` 必须成对打开**：前者让 `hw_clean_to_sv.sh` 放行 extern，后者（`./rtl/fp/circt_fp_primitives.sv`）给这些 extern 提供定点近似实现。少一个都会在 SV 导出站报错。
- 若新模型已**量化为整数**（int8/int4），则**两者都关闭**（与 matmul 同），因为不会有浮点 extern。
- `slangPerFileExternModules` 与浮点无关，只看设计规模：模型大到 Yosys 单次 elaborate 会 OOM 才开。

**自检清单**（写完后对照）：

1. adapter 是否同时定义了 `build_model(model_path)` 与 `example_inputs()`？两者数量/形状/dtype 是否对应？（4.1）
2. `PYTHONPATH` 是否拼了 adapter 兄弟目录 + `torchMlirPythonPath`？（4.3）
3. 权重是否用 `fetchurl` + `linkFarm` 钉成快照、`from_pretrained` 是否带 `local_files_only=True`？（4.3）
4. 浮点模型是否成对开了 `allowHwExterns` + `fpPrimsSv`？（4.2）
5. 大模型是否开了 `slangPerFileExternModules`？（4.2）

> 注：本任务为设计型实践，**不要求实际 `nix build`**（真实小模型仍可能超配，见 u6-l4 的瓶颈结论）。若想验证注册条目本身能被 Nix 求值，可 `nix eval .#packages.<system>.<某输出>.outPath --dry-run` 看依赖图是否连得上。

## 6. 本讲小结

- **加新模型只需两步**：写 Python adapter（`build_model` + `example_inputs`，可选 `EXPORT_STRICT`），在 `nix/models.nix` 加一条 `registerModel { ... }`；降级链的 9 个脚本与 10 个派生自动复用，引擎不用碰。
- **adapter 契约的强制执行方是 `compile-pytorch.py`**：缺 `build_model`/`example_inputs` 会在 [compile-pytorch.py:43-46](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L43-L46) 报错；`example_inputs` 的数量/形状/dtype 必须与 `forward` 形参一一对应。
- **三个流水线开关按问题维度独立设置**：`allowHwExterns` + `fpPrimsSv`（成对）解决「浮点 extern」，服务 SV 导出站；`slangPerFileExternModules` 解决「设计太大、slang OOM」，服务 Yosys 前端站。matmul 全关、TinyStories 全开。
- **开关通过 `optionalString` 注入环境变量**，因此开关状态进入派生 hash，不同配置互不污染缓存（u3-l5 的缓存隔离在注册层的体现）。
- **Python 环境、torch-mlir 绑定、权重快照三样东西的绑定是可复现性的命门**：`python.withPackages` 控制库、`PYTHONPATH` 拼 `torchMlirPythonPath` 让 `import torch_mlir.fx` 可解析、`fetchurl` + `linkFarm` 把权重钉成本地目录、`local_files_only=True` 禁止联网。
- **入口与引擎分离**：`models.nix` 是唯一要改的入口，`pipeline.nix` 是不用动的引擎——这套分层让二次开发的改动面最小。

## 7. 下一步学习建议

- **想验证你注册的新模型能跑通等价性？** 继续看 [u4-l1](u4-l1-golden-reference-and-vectors.md) / [u4-l2](u4-l2-verilator-sim-and-waveform.md)（黄金参考与 Verilator 仿真），为新模型写 testbench，把它的 SV 仿真结果与 PyTorch 黄金参考比对。
- **想给新模型生成上板自测外壳？** 看 [u6-l1](u6-l1-selftest-wrapper-autogen.md)（`gen_tiny_stories_selftest_top.py` 自动按 `main.sv` 端口生成 wrapper），理解端口变化时如何自动重派生外壳。
- **想理解综合与资源报告如何挂在注册链下游？** 看 [u5-l2](u5-l2-matmul-synth-and-bitstream.md) / [u5-l3](u5-l3-utilization-report.md)，以及 `flake.nix` 里 `mkTinyStoriesSelftestBundle` 如何把 `pipeline.il` 接到分阶段综合与资源报告（[flake.nix:566-606](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L566-L606)）。
- **想看清注册链的缓存与惰性机制（本讲多次引用）？** 回看 [u3-l5](u3-l5-pipeline-nix-orchestration.md)，理解 `mkPipeline` 的 `let self = {...}; in self;` 为何让「取 `.il` 不触发 `.sv`」。
- **建议动手顺序**：先用 matmul 跑通 `nix build .#matmul-selftest-bitstream` 确认环境 OK → 仿照本讲综合实践写一个小模型 adapter → `nix eval --dry-run` 验证注册条目可求值 → 最后再关心资源超配问题（u6-l4）。
