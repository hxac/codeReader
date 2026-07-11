# nix/pipeline.nix：把降级链编排成可缓存派生

## 1. 本讲目标

学完本讲，你应当能够：

- 解释为什么 LLM2FPGA 要把 9 个降级 shell 脚本逐个包成 Nix **派生（derivation）**，而不是写一个大脚本一次跑完。
- 看懂 `runCommand` 如何用一个 `$out`、一段 bash、一组 `buildInputs` 把「一个脚本 + 一个输入文件 + 一个可执行文件」变成一个可缓存的产物。
- 读懂 `mkPipeline` 里 `let self = { ... }; in self;` 这种**惰性自引用（lazy self-reference）**技巧，理解它如何把 `torch → linalg → cf → handshake → hs-ext → hw0 → hw → hw-clean → sv → il` 这 10 个阶段串成一条链，并且按需取用。
- 明白 Nix 的**内容寻址缓存**为什么让你「改了 linalg 阶段，只重算 linalg 及其下游，torch 阶段不重算」。
- 会用 `registerModel` 把一个新模型接入这条链，并能预测改某一阶段后哪些产物会重建。

本讲是**编排层（orchestration）**的讲义。前面 u3-l1 到 u3-l4 已经讲清每个降级脚本**内部**做了什么；本讲不讲算子、不讲 dialect，只讲**这些脚本如何被 Nix 组装成一条可缓存的流水线**。

## 2. 前置知识

本讲不要求你精通 Nix，但需要几个概念。下面用最朴素的语言解释。

### 2.1 派生（derivation）

Nix 里一切构建产物（一个文件、一个目录、一个工具）都是**派生**。一个派生由三样东西定义：

1. 一段构建脚本（通常是一小段 bash）。
2. 它依赖的所有输入（其他派生的路径、源文件）。
3. 一个输出名 `name`。

Nix 会把这些输入的「指纹」一起算进派生的哈希，然后把产物存在一个形如 `/nix/store/xxxxxx-名字` 的目录里。**输入不变 → 哈希不变 → 直接复用旧产物，不重算**。这就是 Nix 可复现和可缓存的根。

### 2.2 runCommand

`runCommand name { buildInputs = [...]; } ''脚本''` 是 Nix 最简单的造派生方式：

- `name`：产物名（会成为 store 路径的一部分）。
- `{ buildInputs = [...]; }`：构建时需要用到的工具，Nix 会把它们放进 `PATH`。
- 脚本：一段 bash。Nix 会**自动**给你一个变量 `$out`，它是这次构建的输出路径；你的脚本只需把成果写到 `$out`，脚本结束、`$out` 存在，构建就算成功。

可以把它理解成：「给我一个文件名、一个工具箱、一段把原料加工成成品的指令，我还你一个可缓存的产物」。

### 2.3 惰性求值（lazy evaluation）

Nix 是**惰性**的：一个值只有在被真正用到时才会被计算。这对本讲至关重要——`mkPipeline` 返回的 `self` 属性集里列了 10 个阶段，但你**只取哪个阶段，Nix 就只构建到哪个阶段**。例如只取 `.sv`，那么 `il`（再下一步的综合）根本不会被构建。

### 2.4 let 的自引用

Nix 的 `let` 是**递归绑定（letrec）**：绑定名在自己定义体内就在作用域里。所以可以写出

```nix
let self = { a = 1; b = self.a + 1; }; in self.b
```

`self.b` 求值时会回头找 `self.a`。配合惰性求值，这就能让一串互相依赖的字段在同一个属性集里互相指认。

> 已经学过 u3-l1～u3-l4 的读者：你已经知道 `linalg_to_cf.sh`、`cf_to_handshake.sh` 这些脚本各自做了什么；本讲只关心「谁调用它们、按什么顺序、结果怎么缓存」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [nix/pipeline.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix) | **本讲主角**。把 9 个降级脚本逐个包成 `runCommand` 派生，用 `mkPipeline` 串成链，对外暴露 `registerModel`。 |
| [nix/models.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix) | 用 `registerModel` 注册 `matmul` 与 `tiny-stories-1m-baseline-float` 两个真实模型，给出各自的 PyTorch 前端命令。 |
| [scripts/pipeline/common.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh) | 所有降级脚本共享的三个工具函数：`require_file`、`require_executable`、`run_to_output`。它定义了「输入文件 / 可执行 / 输出」这套统一参数约定，pipeline.nix 正是据此调用脚本。 |
| [flake.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix)（第 227–246 行） | 把 pipeline.nix 和 models.nix import 进来，并暴露 `matmulSv = modelRegistry.matmul.pipeline.sv` 等产物给 `nix build`。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先讲**单站**怎么包成派生（4.1），再讲**全链**怎么串起来（4.2），最后讲**对外接口**怎么统一注册模型（4.3）。

### 4.1 runCommand：把每个降级脚本包成一个派生

#### 4.1.1 概念说明

回顾前面几讲：降级链上每一站都是一个独立的 shell 脚本，比如 `torch_to_linalg.sh` 把 torch 方言 MLIR 降到 Linalg，`linalg_to_cf.sh` 再把 Linalg 降到 CF。这些脚本都遵守 [common.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh) 定义的统一三参约定（见 [common.sh:4-24](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh#L4-L24)）：

```
某站.sh <可执行> <输入文件> <输出文件>
```

- 脚本先用 `require_executable` / `require_file` 检查可执行与输入是否存在，不存在就走 stderr 报错并 `exit 2`；
- 再用 `run_to_output "$output" <可执行> <输入> ...pass...` 把可执行的 stdout 重定向到输出文件。

既然每一站的**形状都一样**（一个可执行、一个输入、一个输出），我们就可以用同一个模式把它们逐个包成 Nix 派生：每个派生的输入是「上一站的输出文件（一个 store 路径）」，输出是「本站的产物（写到 `$out`）」。

**为什么要逐站成派生，而不是一个大脚本跑完？** 核心动机就是**缓存粒度**。降级链很长、每一步都可能很慢（TinyStories-1M 的某些站要跑很久）。如果一条大命令跑到底，改任何一处都要从头重来；逐站成派生后，Nix 会把每一站单独缓存——改了某一站，只有它和它下游重算，上游不动。

#### 4.1.2 核心流程

每个 `mkXxxDerivation` 函数都遵循同一个套路，伪代码如下：

```
输入: { name, 上一站的产物 store 路径 }
1. pkgs.runCommand "${name}-<阶段>.mlir" { buildInputs = [本站需要的工具] } ''
2.     bash  scripts/pipeline/<本站>.sh  <可执行>  <上一站产物>  "$out"
3. ''
```

要点：

1. **产物名**直接由 `name + 阶段后缀` 拼成，例如 `name="matmul"` 时 Linalg 站产物名是 `matmul-linalg.mlir`。
2. **`buildInputs`** 只放本站真正用到的工具（`torchMlir`、`mlir`、`circt`、`yosysPkg` 等），不贪多——这既保证可复现，也让缓存键尽可能稳定。
3. **`"$out"`** 是 Nix 自动注入的输出路径，作为脚本的第 3 个位置参数（输出文件）传入，正好匹配 common.sh 的约定。
4. **上一站产物**作为第 2 个参数传入，它是一个 store 路径（如 `/nix/store/...-matmul-linalg.mlir`）。Nix 把这个路径计入本派生的输入指纹，于是形成了**依赖**。

最终这 10 站的产物名与对应函数一览（以 `matmul` 为例）：

| 阶段字段 | 构建函数 | 产物名（`name="matmul"`） | 依赖的脚本 |
| --- | --- | --- | --- |
| `torch` | `mkTorchInput` | `matmul-torch.mlir` | `compile-pytorch.py`（前端，非降级） |
| `linalg` | `mkLinalgDerivation` | `matmul-linalg.mlir` | `torch_to_linalg.sh` |
| `cf` | `mkCfDerivation` | `matmul-cf.mlir` | `linalg_to_cf.sh` |
| `handshake` | `mkHandshakeDerivation` | `matmul-handshake.mlir` | `cf_to_handshake.sh` |
| `hs-ext` | `mkHsExtDerivation` | `matmul-hs-ext.mlir` | `handshake_to_hs_ext.sh` |
| `hw0` | `mkHw0Derivation` | `matmul-hw0.mlir` | `hs_ext_to_hw0.sh` |
| `hw` | `mkHwDerivation` | `matmul-hw.mlir` | `hw0_to_hw.sh` |
| `hw-clean` | `mkHwCleanDerivation` | `matmul-hw-clean.mlir` | `hw_to_hw_clean.sh` |
| `sv` | `mkSvDerivation` | `matmul-sv` | `hw_clean_to_sv.sh` |
| `il` | `mkIlDerivation` | `matmul.il` | `sv_to_il.sh` |

#### 4.1.3 源码精读

我们挑三个有代表性的站来看：Linalg（最简单）、SV（带可选环境变量）、IL（综合，参数最多）。

**Linalg 站**——最朴素的「一个可执行 + 一个输入 + `$out`」：

[nix/pipeline.nix:30-34](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L30-L34) —— 产物名 `matmul-linalg.mlir`；`buildInputs = [ torchMlir ]`；用 `${torchMlirOpt}`（torch-mlir 的优化器二进制）作为可执行，`${torch}` 作为输入，`"$out"` 作为输出。注意 `${torchMlirOpt}` 并非凭空而来，它是在文件顶部 [nix/pipeline.nix:3-13](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L3-L13) 用候选路径列表 `builtins.filter builtins.pathExists` 找出来的——因为 torch-mlir 这个 Python 包把 `torch-mlir-opt` 放在 site-packages 下，位置随安装方式变化，所以列三个候选取第一个存在的。这是一个很实用的「跨包定位二进制」技巧。

**SV 站**——演示如何用 `optionalString` 按需注入环境变量：

[nix/pipeline.nix:74-84](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L74-L84) —— 当 `allowHwExterns` 为真时才 `export ALLOW_HW_EXTERNS=1`，当 `fpPrimsSv` 非 null 时才 `export FP_PRIMS_SV=...`。这两个开关直接对应 u3-l4 讲过的「禁止裸 extern」安全门：默认拒绝 extern，只在确实提供了浮点原语实现（`fpPrimsSv`）并显式放行（`allowHwExterns`）时才让 HW→SV 导出通过。**关键是这两个开关会被算进派生指纹**——同一个 `hwClean`，开关不同会得到不同的 `sv` 产物，互不污染缓存。

**IL 站**——综合，参数最多，能看清脚本统一约定：

[nix/pipeline.nix:86-95](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L86-L95) —— 这里调用的是 `sv_to_il.sh`，传入的参数依次是 `${yosysPkg}/bin/yosys`（可执行）、`${yosysSlang}/.../slang.so`（slang 插件）、`${sv}/sources.f`（输入——注意这里不是单个 .sv 文件，而是 HW→SV 那一站 [u3-l4] 产出的**文件清单** `sources.f`）、`"$out"`（输出，一个 `.il` 即 RTLIL）。`slangPerFileExternModules` 开关同样会 `export YOSYS_SLANG_PER_FILE_EXTERNS=1`，对应 u5-l1 要讲的「按文件 extern」模式。

**前端入口 `mkTorchInput`**——唯一不「降级」的站，它把 PyTorch 模型导出成 torch 方言 MLIR：

[nix/pipeline.nix:15-28](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L15-L28) —— 这个函数比降级站灵活：它接受**两种**输入。如果调用方已经有一个现成的 torch MLIR 派生（`torchMlirInput`），就直接复用；否则接受一段 `torchInputCommand`，用 `runCommand` 跑这段命令来产出 `${name}-torch.mlir`。如果两者都没给，就 `throw` 一个明确错误。这种「要么给现成产物、要么给一段命令」的设计，让 `matmul`（用命令实时算）和未来可能缓存好 MLIR 的模型都能接进来。

#### 4.1.4 代码实践

**实践目标**：亲手验证「每个降级脚本都遵守同一个三参约定」，并理解 pipeline.nix 是如何按这个约定调它们的。

**操作步骤**：

1. 打开 [nix/pipeline.nix:36-40](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L36-L40)（`mkCfDerivation`），确认它调用 `linalg_to_cf.sh` 时传了三个参数：`${mlir}/bin/mlir-opt`、`${linalg}`、`"$out"`。
2. 打开 [scripts/pipeline/linalg_to_cf.sh:9-10](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/linalg_to_cf.sh#L9-L10)，确认脚本把 `$1` 当 `mlir-opt`、`$2` 当输入、`$3` 当输出（用法串里写着 `<mlir-opt> <input-linalg-mlir> <output-cf-mlir>`）。
3. 同样对照 `mkHandshakeDerivation`（[nix/pipeline.nix:42-48](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L42-L48)）——它是**唯一**一次给脚本传**两个**可执行（`circt-opt` 和 `mlir-opt`），因为 `cf_to_handshake.sh` 这一站需要同时用到 CIRCT 和上游 MLIR 两个工具（回顾 u3-l2）。数一下它传给脚本的参数个数。

**需要观察的现象**：除了 handshake 站因需要两个可执行而多一个参数外，其余降级站都是「一个可执行 + 一个输入 + 一个输出」的固定三参形状。

**预期结果**：你会确认 pipeline.nix 里 9 个 `mkXxxDerivation` 本质上是同一个模板的 9 次实例化，差异只在「用哪个可执行、用哪个脚本、需要哪些 `buildInputs`」。

> 说明：本实践是源码阅读型，不依赖完整 Nix 构建；如本地尚未配好 Nix，可只做对照阅读。

#### 4.1.5 小练习与答案

**练习 1**：`mkHandshakeDerivation` 的 `buildInputs` 是 `[ mlir circt ]`（[nix/pipeline.nix:44](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L44)），而前后几站大多只有一个工具。为什么 handshake 站需要两个？

**参考答案**：因为 `cf_to_handshake.sh` 这一站同时调用 `circt-opt`（做 flatten-memref / handshake 合法化 / lower-cf-to-handshake / 插缓冲）和 `mlir-opt`（做 convert-scf-to-cf 清理），是全链唯一同时用 CIRCT 和上游 MLIR 的站（详见 u3-l2）。`buildInputs` 把这两个工具都放进构建环境。

**练习 2**：如果把 `mkSvDerivation` 里的 `allowHwExterns` 从 `false` 改成 `true`（且 `fpPrimsSv` 给定），同一个 `hwClean` 输入下，新旧两个 `sv` 产物的 store 路径会相同吗？

**参考答案**：不会相同。`optionalString allowHwExterns ''export ALLOW_HW_EXTERNS=1''` 会改变构建脚本内容，进而改变派生的指纹（哈希），于是得到一个不同的 store 路径。这正是把开关算进指纹带来的好处：不同配置的产物在缓存里互不覆盖。

---

### 4.2 mkPipeline：惰性自引用串起整条降级链

#### 4.2.1 概念说明

4.1 里我们把每一站都写成了一个独立函数（`mkLinalgDerivation`、`mkCfDerivation`……）。但调用方（flake.nix）想要的不是「分别调 10 个函数」，而是**一条已经串好的链**：给它一个 torch 输入，它返回一个属性集，里面 `linalg` 已经喂给了 `cf`、`cf` 已经喂给了 `handshake`……一直串到 `il`。

`mkPipeline` 就是干这件事的。它的妙处在于：用一个 `let self = { ... }; in self;` 的**惰性自引用**，在一个属性集里让每个字段指向「前一个字段」，从而把 10 个独立函数焊成一条链。

更重要的是**惰性**：返回的 `self` 里有 `sv` 也有 `il`，但你只取 `.sv` 时，`il` 那一站（综合，最慢）**根本不会被求值**。这就是为什么 flake.nix 暴露 `matmulSv = modelRegistry.matmul.pipeline.sv` 时，不会顺带把更贵的 `il` 也构建出来。

#### 4.2.2 核心流程

`mkPipeline` 的求值可以想象成「声明一张链表，但每个节点只在被碰到时才生成」：

```
mkPipeline { name, torch, ...开关 } =
  let
    self = {
      torch     = torch                      # 链头：外部给定的 torch MLIR
      linalg    = mkLinalgDerivation   { name; torch = self.torch; }
      cf        = mkCfDerivation       { name; linalg = self.linalg; }
      handshake = mkHandshakeDerivation{ name; cf = self.cf; }
      hs-ext    = mkHsExtDerivation    { name; handshake = self.handshake; }
      hw0       = mkHw0Derivation      { name; hsExt = self."hs-ext"; }
      hw        = mkHwDerivation       { name; hw0 = self.hw0; }
      hw-clean  = mkHwCleanDerivation  { name; hw = self.hw; }
      sv        = mkSvDerivation       { name; hwClean = self."hw-clean"; 开关; }
      il        = mkIlDerivation       { name; sv = self.sv; 开关; }
    }
  in self
```

由于 Nix 的 `let` 是递归绑定，`self.linalg` 在定义体内就能引用 `self.torch`；又由于惰性，只有当外部真正访问某个字段（比如 `self.sv`）时，才会沿着 `sv → hw-clean → hw → hw0 → hs-ext → handshake → cf → linalg → torch` 这条反向链一路求值下去，被访问到的站才构建，没被碰到的站（如 `il`）不构建。

#### 4.2.3 源码精读

[nix/pipeline.nix:97-141](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L97-L141) —— 这就是 `mkPipeline` 的全部。几个值得注意的细节：

1. **`let self = { ... }; in self;`**（第 99–141 行）：`self` 是一个普通属性集，但它在自己体内被引用，靠的就是 Nix letrec 语义。这是「把一串互相依赖的派生收进一个递归属性集」的惯用法。
2. **带引号的字段名**：`"hs-ext"` 和 `"hw-clean"` 因为名字里有连字符，必须用字符串键（[第 114、126 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L114-L126)）。引用时也得用点字符串：`self."hs-ext"`（[第 120 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L120)），不能用 `self.hs-ext`（会被解析成减法）。
3. **开关的透传**：`allowHwExterns`、`fpPrimsSv` 只在 `sv` 站用（[第 130–134 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L130-L134)），`slangPerFileExternModules` 只在 `il` 站用（[第 135–139 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L135-L139)）。这些开关从 `mkPipeline` 一路 `inherit` 下去，最终落到对应站的 `optionalString` 上。

**关于惰性的实际后果**——看 flake.nix 怎么取用：

[flake.nix:244-246](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L244-L246) —— `matmulSv` 只取到 `.pipeline.sv`，所以 `nix build .#matmulSv` 只会构建到 SV 站（torch→…→sv），不会去碰 `il` 综合；而 `tinyStories1mBaselineFloatIl` 取到 `.pipeline.il`，才会一路构建到 RTLIL。这正是惰性求值在工程上的价值：**你需要多深的产物，Nix 就只构建多深**。

#### 4.2.4 代码实践

**实践目标**：理解「改了 linalg 阶段后，哪些阶段重算、哪些不会」——这是 Nix 缓存的核心收益。

**操作步骤（源码推理）**：

1. 假设你修改了 `scripts/pipeline/torch_to_linalg.sh`（比如给它加一行 `-debug`），别的不动。
2. 追踪这次改动影响了哪个派生的指纹：只有 `mkLinalgDerivation` 的脚本内容变了（因为它直接 source/调用 `torch_to_linalg.sh`，脚本路径通过 `${pipelineScripts}` 进入构建）。
3. 沿 `self` 链回答：`linalg` 变了 → `cf`（以 `self.linalg` 为输入）变 → `handshake` 变 → … → `sv` 变 → `il` 变。
4. 而 `torch` 站呢？它的输入是 adapter、matmul.py、compile-pytorch.py、pythonWithTorch，这些**一个都没变**，所以 `torch` 的指纹不变 → 不重算。

**需要观察的现象（待本地验证）**：执行 `nix build .#matmulSv -L`，构建日志里应能看到 `torch` 阶段是缓存命中（如 `got: /nix/store/...` 而非重新构建），而从 `linalg` 起各站重新构建。

**预期结果（按 Nix 语义）**：

- **会重算**：`linalg`、`cf`、`handshake`、`hs-ext`、`hw0`、`hw`、`hw-clean`、`sv`（以及如果取了 `il` 则 `il` 也重算）。
- **不会重算**：`torch`（前端导出）——因为它的输入闭包未变。

> 提示：可以用 `nix derivation show .#matmulSv`（待本地验证具体子路径）查看 `inputDrvs`，能直观看到 `sv` 派生直接依赖哪些 store 路径，从而验证这条链。

#### 4.2.5 小练习与答案

**练习 1**：flake.nix 里写 `matmulSv = modelRegistry.matmul.pipeline.sv`。如果你**只**运行 `nix build .#matmulSv`，`matmul.il` 会被构建吗？为什么？

**参考答案**：不会。Nix 是惰性的，`pipeline.il` 只有在被某处求值时才会构建。`matmulSv` 只引用了 `.pipeline.sv`，没有碰 `.il`，所以 `mkIlDerivation` 这一站根本不会被求值，也就不会触发综合。

**练习 2**：`mkPipeline` 里为什么不直接写 `cf = mkCfDerivation linalg`（把上一站当位置参数传），而要绕一圈用 `let self = {...}; in self`？

**参考答案**：因为 `mkXxxDerivation` 接受的是命名参数属性集（`{ name, linalg }`），而且更重要的是要在**同一个属性集**里让任意字段都能引用其他字段、还能整体作为 `pipeline` 返回给外部按名取用。`let self` 的自引用让 10 个站可以在一处集中声明、互相指认、且惰性求值；若用嵌套的 `let ... in` 一层层手写依赖，既冗长又难以整体暴露成 `pipeline.sv` / `pipeline.il` 这种统一接口。

---

### 4.3 registerModel：统一注册模型

#### 4.3.1 概念说明

`mkPipeline` 解决了「一条链怎么串」，但它要求调用方先准备好一个 `torch` 输入。而对一个**真实模型**来说，「torch 输入怎么来」本身就有讲究：matmul 是一条 Python 命令实时算出来的；TinyStories-1M 要加载 HuggingFace 权重再导出；将来可能有模型直接给一份现成的 MLIR。

`registerModel` 就是把这层差异**归一**的统一入口：它接受「要么现成 MLIR、要么一段命令」，先用 `mkTorchInput` 把它统一成一个 `torch` 派生，再交给 `mkPipeline` 串完整条链，最后返回 `{ pipeline = ...; }`。

这样一来，`models.nix` 里每注册一个模型，只要写一段 PyTorch 前端命令 + 几个开关，就能自动获得从 torch 一路到 il 的完整可缓存链——**这就是「统一注册」的含义**。

#### 4.3.2 核心流程

```
registerModel { name, torchMlirInput?, torchInputCommand?, ...开关 } =
  let
    torch = mkTorchInput { name; torchMlirInput?; torchInputCommand?; ... }
  in {
    pipeline = mkPipeline { name; torch; allowHwExterns?; fpPrimsSv?; slangPerFileExternModules? }
  }
```

两个阶段：

1. **前端归一**：`mkTorchInput` 产出 `torch`（一个 store 路径，指向 torch 方言 MLIR 文件）。
2. **全链串联**：把 `torch` 和各开关喂给 `mkPipeline`，得到完整的 `pipeline` 属性集。

调用方拿到 `{ pipeline = self; }` 后，用 `pipeline.sv`、`pipeline.il` 等按需取用（惰性）。

#### 4.3.3 源码精读

**registerModel 主体**：

[nix/pipeline.nix:143-154](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L143-L154) —— 注意几个设计要点：

- **可选参数用 `? null` 默认值**（`torchMlirInput ? null` 等），这样调用方只给需要的部分。matmul 只给 `torchInputCommand`；TinyStories 还额外给 `allowHwExterns = true`、`slangPerFileExternModules = true`、`fpPrimsSv`。
- **`torch` 在 `let` 里先求出来**（第 147–149 行），再传给 `mkPipeline`。这是因为 `mkPipeline` 的 `torch` 形参需要一个确定的值，而 `mkTorchInput` 恰好负责把「两种输入」归一成这一个值。
- **返回值是 `{ pipeline = ...; }`**（第 150–154 行）。注意它没有把 `torch` 单独暴露出去——调用方只关心整条 `pipeline`，前端产物被视为内部细节。

**两个真实模型的注册**——看 models.nix 怎么用：

[nix/models.nix:8-18](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L8-L18) —— `matmul` 的注册：只给 `name`、`torchInputBuildInputs = [ pythonWithTorch ]` 和一段 `torchInputCommand`。这段命令设好 `MATMUL_PY`、`PYTHONPATH`，然后 `python compile-pytorch.py --adapter ... --out "$out"`。这段命令就是 u2-l2 讲过的前端导出，`$out` 直接成为 `matmul-torch.mlir`。注意 `>/dev/null`——前端脚本的日志被丢弃，只留 MLIR 文件到 `$out`。

[nix/models.nix:20-33](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L20-L33) —— `tiny-stories-1m-baseline-float` 的注册：命令更长（要 `--model-path` 指向离线权重快照 `${tinyStories1m.snapshot}`，回顾 u2-l1），并且**打开了三个开关**：`allowHwExterns = true`、`slangPerFileExternModules = true`、`inherit fpPrimsSv`。这三个开关分别传到 `sv` 站（放行浮点 extern 并挂接原语）和 `il` 站（按文件 extern 综合模式）。对比 matmul（**不**带任何开关），你能清楚地看到：**普通算子核用默认严格模式即可，真实大模型因为带浮点算子、带超大存储，才需要打开这些「放行」开关**。

**flake.nix 的接线**——把库和注册表 import 起来并暴露产物：

[flake.nix:227-246](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L227-L246) —— 两步：

1. 第 227–230 行：`import ./nix/pipeline.nix { ... }` 得到 `pipelineLib`，其中只有 `registerModel` 这一个函数被对外使用（pipeline.nix 最后 `in { inherit registerModel; }`，见 [nix/pipeline.nix:156](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L156)）。
2. 第 232–242 行：`import ./nix/models.nix { inherit (pipelineLib) registerModel; ... }` 得到 `modelRegistry`，它是一个含 `matmul` 和 `"tiny-stories-1m-baseline-float"` 两个键的属性集，每个值都是 `{ pipeline = self; }`。
3. 第 244–246 行：从注册表里取产物暴露成 flake 的 package：`matmulSv = modelRegistry.matmul.pipeline.sv`、`tinyStories1mBaselineFloatIl = modelRegistry."tiny-stories-1m-baseline-float".pipeline.il`。

于是 `nix build .#matmulSv` 命中的正是这条链的 `sv` 站产物。这就是 u1-l4 里那条 gate 命令背后「降级链」的真正组装方式。

#### 4.3.4 代码实践

**实践目标**：跟踪 `matmul` 模型从 torch 到 sv 的派生依赖链，列出每个阶段的派生名，并解释前端命令如何成为链头。

**操作步骤**：

1. 从 [nix/models.nix:8-18](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L8-L18) 出发：matmul 经 `registerModel` 进入，其 `torchInputCommand` 跑 `compile-pytorch.py` 产出 `$out`。
2. 这个 `$out` 经 [nix/pipeline.nix:20](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L20) 的 `runCommand "${name}-torch.mlir"` 成为派生 **`matmul-torch.mlir`**（链头）。
3. 沿 `self`（[nix/pipeline.nix:100-141](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L100-L141)）逐站写出派生名（见 4.1.2 的表格）。

**matmul 从 torch 到 sv 的派生名链**：

```
matmul-torch.mlir     (前端导出)
  → matmul-linalg.mlir
    → matmul-cf.mlir
      → matmul-handshake.mlir
        → matmul-hs-ext.mlir
          → matmul-hw0.mlir
            → matmul-hw.mlir
              → matmul-hw-clean.mlir
                → matmul-sv   ← nix build .#matmulSv 取到这一站
```

（再往下还有 `matmul.il`，但 `.sv` 不会触发它。）

**需要观察的现象（待本地验证）**：在配好 Nix 的环境里执行 `nix path-info .#matmulSv --derivation --recursive`（命令名待本地验证），可以列出从 `matmul-sv` 一直回溯到 `matmul-torch.mlir` 的全部派生路径，与上面的链一一对应。

**预期结果**：你会看到一条 9 层（torch→…→sv）的单链依赖，没有分叉——因为每一站都只依赖上一站的唯一产物。这正是 `mkPipeline` 用线性 `self` 引用构造出来的形状。

#### 4.3.5 小练习与答案

**练习 1**：matmul 注册时**没有**给 `allowHwExterns`，TinyStories 注册时给了 `allowHwExterns = true`。如果一个模型不带浮点算子、也没有超大存储，它需要打开这些开关吗？

**参考答案**：不需要。这些开关（`allowHwExterns` / `fpPrimsSv` / `slangPerFileExternModules`）是为了放行 CIRCT 降级时产生的「裸 extern」（主要是浮点算子）和处理超大存储的按文件 extern 综合模式。matmul 是纯整数 matmul，降级后 HW 里没有需要挂接的 extern，所以用默认严格模式（拒绝裸 extern）即可，更安全。开关按需打开，正是「最小放行」的安全设计。

**练习 2**：`registerModel` 返回的是 `{ pipeline = self; }`，没有把 `torch` 单独放进返回值。如果你想单独拿到某个模型的 `linalg` 产物来调试，应该怎么做？

**参考答案**：直接走 pipeline 属性：`modelRegistry.matmul.pipeline.linalg`。因为 `self` 属性集里每一站（包括中间的 `linalg`、`cf` 等）都是可访问的字段，且惰性求值——取 `.linalg` 只会构建到 Linalg 站。所以调试某一站时，可在 flake.nix 临时加一个 `matmulLinalg = modelRegistry.matmul.pipeline.linalg;` 暴露出来，用 `nix build .#matmulLinalg` 单独取那一站的 MLIR，不必跑到 sv。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来——验证「改 linalg 阶段只重算下游」这条核心结论，并亲手暴露一个中间产物来调试。

**步骤**：

1. **读链**：从 [nix/models.nix:8-18](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L8-L18) → [nix/pipeline.nix:143-154](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L143-L154)（registerModel）→ [nix/pipeline.nix:97-141](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L97-L141)（mkPipeline）→ [nix/pipeline.nix:30-34](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L30-L34)（mkLinalgDerivation）一路追下来，画出 matmul 的完整依赖链（torch→il 共 10 站，标注每站的派生名与所用脚本）。

2. **暴露中间产物**：在 [flake.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix) 的 `packages` 里临时加一行 `matmulLinalg = modelRegistry.matmul.pipeline.linalg;`（参照第 244 行 `matmulSv` 的写法）。这样 `nix build .#matmulLinalg` 就只会构建到 Linalg 站，方便你单独检查那份 MLIR。

3. **预测重算范围**：假设你修改了 `linalg_to_cf.sh`（给它加一个注释行，不影响逻辑），回答：
   - 哪些站会重算？（答：cf 及其下游 handshake→…→il；linalg 本身不变，因为它的脚本 torch_to_linalg.sh 没动。）
   - 哪些站不重算？（答：torch、linalg；因为它们的输入闭包未变。）

4. **验证（待本地验证）**：在配好 Nix 的环境里，先 `nix build .#matmulSv -L` 建立基线缓存；改 `linalg_to_cf.sh` 加一行注释；再 `nix build .#matmulSv -L`。观察日志，确认从 cf 起各站重新构建，而 torch / linalg 命中缓存。

**预期结果**：你会亲眼看到 Nix 的「逐站缓存 + 线性依赖链」带来的收益——只改一站，只重算它及下游，上游前端导出（往往最依赖重量级 PyTorch / torch-mlir）完全复用。

## 6. 本讲小结

- **runCommand 是缓存的基本单元**：每个降级脚本被包成一个 `runCommand` 派生，产物名形如 `${name}-<阶段>.mlir`；`buildInputs` 放工具、`$out` 当输出、上一站产物当输入，三参约定源自 [common.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh)。
- **mkPipeline 用惰性自引用串链**：`let self = { ... }; in self;` 让 10 个站在一个递归属性集里互相指认，按需取用——取 `.sv` 不会触发 `.il`。
- **开关被算进指纹**：`allowHwExterns` / `fpPrimsSv` / `slangPerFileExternModules` 通过 `optionalString` 注入环境变量，不同配置得到不同 store 路径，互不污染缓存。
- **registerModel 归一前端**：把「现成 MLIR 或一段命令」统一成 `torch`，再喂给 `mkPipeline`，返回 `{ pipeline = self; }`；models.nix 用它注册 matmul（无开关）与 TinyStories（三开关全开）。
- **缓存收益**：改某一站只重算该站及其下游，上游不动；这条线性链的形状直接来自 `self` 的一对一字段引用。
- **flake.nix 的接线**：`pipelineLib`（只暴露 `registerModel`）→ `modelRegistry`（每模型一个 `{ pipeline; }`）→ `matmulSv` / `tinyStories1mBaselineFloatIl` 等产物暴露给 `nix build`。

## 7. 下一步学习建议

- **向后看综合**：本讲停在 `il`（RTLIL）。下一单元 u5 会从 `sv` / `il` 出发，讲 Yosys + slang 如何把 SystemVerilog 综合成 RTLIL、如何做 `synth_xilinx` 出 JSON，以及 matmul 如何一路走到比特流（u5-l1、u5-l2）。
- **看资源报告**：u5-l3 讲 `write_utilization_report.py` 如何把综合后的 JSON 统计成 `summary.txt`——那正是 u1-l4 gate 命令产物背后的统计逻辑。
- **二次开发**：想接入新模型，回到本讲的 `registerModel`（[nix/pipeline.nix:143-154](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L143-L154)）与 models.nix 的两个例子，按 u7-l1 的「adapter + 注册两步法」操作即可自动获得整条可缓存链。
- **建议精读**：若想巩固 Nix 基础，重点理解本讲的两个惯用法——`runCommand` 的 `$out` 机制，和 `let self = {...}; in self` 的惰性递归属性集；它们是读懂整个 `nix/` 目录的钥匙。
