# Nix 与可复现工具链入门

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 LLM2FPGA 为什么用 **Nix flake** 而不是 `pip` / `conda` 来管理环境。
- 打开 [flake.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix)，看懂它的 `inputs` 块如何把 CIRCT、Yosys、torch-MLIR 等工具「钉死」在具体的 commit 上。
- 理解 `devShells.default` 提供了哪些工具，并能用 `nix develop` 进入这个带全套工具的 shell。
- 读懂本讲最关键的一句注释：**为什么 torch-MLIR 的 LLVM 要单独 pin**。

本讲不要求你会写 Nix，只要能「读懂」flake.nix 的结构即可。

## 2. 前置知识

### 2.1 什么是「可复现（reproducible）」

「可复现」的意思是：**任何人、在任何机器上、按照同样步骤，都能得到完全一样的结果**。

对 LLM2FPGA 来说这特别难，因为它不只是一个 Python 项目。它的工具链横跨好几种语言和好几个互相依赖的编译器：

| 工具 | 作用 | 属于哪一段流水线 |
|------|------|------------------|
| torch-MLIR | 把 PyTorch 图转成 MLIR | PyTorch 前端 |
| CIRCT | 把 MLIR 一路降级到硬件方言 | MLIR 降级链 |
| Yosys + yosys-slang | 把 SystemVerilog 综合成网表 | 综合阶段 |
| openXC7 / nextpnr | 把网表布局布线成比特流 | 比特流生成 |
| Verilator | 仿真 SystemVerilog | 验证阶段 |

这些工具都是 **C++ 编译出来的重型程序**，而且彼此**版本高度敏感**：换一个 CIRCT 版本，降级链就可能崩溃；换一个 Yosys 版本，yosys-slang 插件可能就编译不过。

`pip` 和 `conda` 能很好地管理 Python 包，但它们管不了「从某个特定 git commit 编译 CIRCT」这种事。这正是 Nix 登场的理由。

### 2.2 Nix 与 flake 的三个关键词

- **Nix**：一个「函数式」的包管理器。它把每一个包都构建成一份只读、带哈希的产物，放在 `/nix/store/` 下。因为路径里带哈希，所以同一个包的不同版本可以共存、互不干扰。
- **flake**：一个 Nix 项目的标准入口。它就是仓库根目录下一个叫 `flake.nix` 的文件，加上一个自动生成的 `flake.lock`（锁文件）。flake 用 `inputs` 声明依赖，用 `outputs` 声明产物。
- **pin（钉死）**：把某个依赖固定到一个**精确的版本**（通常是一个 git commit 的哈希）。锁文件 `flake.lock` 会把这些 commit 全部记下来，从而保证可复现。

> 在 u1-l2 里你已经知道：`flake.nix` 位于仓库根目录（而不是 `nix/` 下），`nix/` 目录装的是被 flake 调用的编排逻辑（如 `pipeline.nix`、`models.nix`）。本讲我们就打开根目录的 `flake.nix`。

## 3. 本讲源码地图

本讲主要围绕一个文件，并附带两个佐证文件：

| 文件 | 作用 |
|------|------|
| [flake.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix) | 本讲主角。声明所有工具的 pin、定义 devShell、定义 packages/checks/apps。 |
| [torch-mlir.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/torch-mlir.nix) | 被 flake 调用，描述「如何从源码编译 torch-mlir」。佐证 torch-MLIR 自带 submodule 的 LLVM。 |
| [README.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md) | 给出对外暴露的 `nix build` / `nix run` 命令。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **flake inputs 与版本 pin**——理解依赖是怎么被钉死的。
2. **devShell 与工具集合**——理解如何进入一个带全套工具的 shell。
3. **LLVM/torch-MLIR 的独立 pin 策略**——理解项目里最微妙的一处 pin 设计。

---

### 4.1 flake inputs 与版本 pin

#### 4.1.1 概念说明

一个 flake 的骨架长这样：

```nix
{
  description = "LLM2FPGA";
  inputs = { ... };      # 声明依赖（从哪里、哪个版本拉代码）
  outputs = inputs@{ ... }: { ... };  # 把依赖加工成产物
}
```

- `inputs`：每一个条目都是一个外部依赖，`.url` 指向一个 GitHub 仓库或 git 仓库，可以带 `ref`（分支）或 `rev`（具体 commit）。
- `outputs`：一个函数，接收所有 `inputs`，返回这个 flake 能产出的东西（包、devShell、检查、app 等）。

LLM2FPGA 的核心思路是：**降级链上的几乎每一个工具，都是一个被单独 pin 的 input**。因为这条链对版本极其敏感，所以项目选择从源码、按精确 commit 重建每个工具，而不是用系统自带的版本。

#### 4.1.2 核心流程

flake 的执行流程可以这样理解：

```
flake.lock 锁定每个 input 的精确 commit
        │
        ▼
inputs = { nixpkgs, circt-src, yosys, yosys-slang, openXC7, ... }
        │
        ▼
outputs 函数把 inputs 加工成：
   ├── packages   （可构建产物：比特流、资源报告、仿真结果）
   ├── devShells  （交互式开发环境）
   ├── checks     （CI 静态检查）
   └── apps       （可运行命令，如 docs-md）
```

关键点：`inputs` 只负责「拉源码」，真正「怎么编译」是在 `outputs` 里用各种 `mkDerivation` / `callPackage` 描述的。

#### 4.1.3 源码精读

先看 inputs 块的整体规模：

[flake.nix:L4-L44](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L4-L44) —— 这是整个 flake 的依赖声明区，一共约 10 个 input，每个都对应降级链上的一个工具或一块基础库。

挑几个有代表性的看：

**① 普通 nixpkgs 与「专门为 LLVM 服务的 nixpkgs」并存**

```nix
nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
nixpkgs-llvm21.url =
  "github:NixOS/nixpkgs/346dd96ad74dc4457a9db9de4f4f57dab2e5731d";
```

[flake.nix:L5-L7](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L5-L7) —— 主 `nixpkgs` 跟随 `nixos-24.05`；同时额外 pin 了一个 `nixpkgs-llvm21` 到一个**具体 commit**，专门用来提供 LLVM 21 那套工具链。这就是「pin」最直白的例子：不写 `nixos-24.05` 这种会漂移的通道，而是写死一长串 commit 哈希。

**② `flake = false`：只要源码，我自己编译**

```nix
circt-src = {
  type = "github";
  owner = "RCoeurjoly";
  repo = "circt";
  ref = "task3";
  flake = false;
};
```

[flake.nix:L15-L21](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L15-L21) —— `circt-src` 指向**项目作者自己的 CIRCT fork**，分支是 `task3`（融合了 Task 3 所需的修改）。`flake = false` 的含义是：「我只把这个仓库当**纯源码**拉下来，别把它当 flake 解析，我要在 outputs 里自己定义怎么编译它」。这样项目能完全掌控 CIRCT 的编译选项。

`yosys-slang`（[flake.nix:L11-L14](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L11-L14)）、`nextpnrXilinxFork`（[flake.nix:L35-L39](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L35-L39)）、`ypcbHack`（[flake.nix:L40-L43](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L40-L43)）也都是 `flake = false`，同理。

**③ `follows`：让上游用「我的那份」依赖**

```nix
circt-nix = {
  url = "git+https://github.com/dtzSiFive/circt-nix?ref=main";
  inputs."circt-src".follows = "circt-src";
  inputs."llvm-submodule-src" = {
    type = "github";
    owner = "llvm";
    repo = "llvm-project";
    rev = "972cd847efb20661ea7ee8982dd19730aa040c75";
    flake = false;
  };
};
```

[flake.nix:L22-L32](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L22-L32) —— `circt-nix` 是一个「帮我们编译 CIRCT」的第三方 flake。两行很关键：

- `inputs."circt-src".follows = "circt-src";`：circt-nix 本来自己有一份 circt 源码依赖，这里让它「**跟随**」我们上面定义的那个 `task3` fork，而不是用它自己的版本。于是最终编译出来的 CIRCT 一定是我们的 fork。
- `inputs."llvm-submodule-src" = { ... rev = "972cd847..."; };`：再额外把 CIRCT 依赖的 **LLVM submodule 也 pin 到一个具体 commit**。这是项目里第一个 LLVM pin（给 CIRCT 用的），请记住它，模块 4.3 会拿来对比。

**④ `circt` 派生：干净的上游 + 受控的补丁**

```nix
circtBase =
  (circtPkgs.circt.override { enableSlang = false; }).overrideAttrs
  (old: { patches = old.patches or [ ]; });
# Keep reviewer builds on a pinned upstream CIRCT plus the checked-in
# Task 3 patch stack. Local fast iteration should use local binaries via
# scripts/dev rather than a flake input override.
circt = circtBase;
```

[flake.nix:L53-L59](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L53-L59) —— 这里关掉了 circt-nix 自带的 slang（`enableSlang = false`），并且把 nix 层面的 `patches` 清空（`patches = old.patches or [ ]`）。配合注释可以看出设计意图：**评审/CI 构建要以「pin 好的 task3 fork」为准**，CIRCT 的修改已经烧进 fork 分支本身，而不是靠 nix 层临时打补丁。（补丁栈的细节属于 u6-l4 的内容，本讲只需理解「这里是有意把 CIRCT 钉死并保持干净」即可。）

#### 4.1.4 代码实践

**实践目标**：亲手从源码里读出「每个工具被钉在哪个版本」，而不是靠记忆。

**操作步骤**：

1. 打开 [flake.nix 的 inputs 块](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L4-L44)。
2. 逐个 input 填下面这张表（前两行已示例）：

   | input 名 | 来源仓库 | 版本标识（ref 或 rev） | `flake=false`? |
   |----------|----------|------------------------|----------------|
   | circt-src | RCoeurjoly/circt | ref=task3 | 是 |
   | circt-nix 的 llvm-submodule-src | llvm/llvm-project | rev=972cd847... | 是 |
   | yosys | … | … | … |
   | yosys-slang | … | … | … |
   | openXC7 | … | … | … |
   | nixpkgs-llvm21 | … | … | 否 |

3. 找出所有 `flake = false` 的 input，数一数一共有几个。

**需要观察的现象**：你会发现几乎「能从源码编译的工具」都标了 `flake = false`，而 `nixpkgs`、`nix-eda`、`openXC7`、`circt-nix` 这类「提供现成构建逻辑」的则没有标。

**预期结果**：约 5 个 input 标了 `flake = false`（circt-src、yosys-slang、nextpnrXilinxFork、ypcbHack，以及 circt-nix 内部嵌套的 llvm-submodule-src）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `circt-src` 要指向 `RCoeurjoly/circt` 的 `task3` 分支，而不是 CIRCT 官方仓库的某个 release？

**参考答案**：因为官方 CIRCT 还不能完整支持本项目的降级链（参见 u6-l4 的补丁栈）。作者把 Task 3 需要的修改维护在自己的 fork 的 `task3` 分支上，pin 到这个分支才能保证降级链跑得通；用官方 release 会导致工具崩溃或算子不支持。

**练习 2**：`inputs."circt-src".follows = "circt-src";` 这一行如果删掉，会发生什么？

**参考答案**：circt-nix 会退回到使用它自己默认的 circt 源码依赖，而不是我们的 `task3` fork。那么最终编译出的 CIRCT 就不带本项目需要的修改，降级链会失败。`follows` 的作用就是「强制上游用我指定的这一份」。

---

### 4.2 devShell 与工具集合

#### 4.2.1 概念说明

flake 的 `outputs` 里有几种产物，本讲关注两种：

- **`packages`**：用 `nix build` 构建出来的「产物」（比特流、资源报告等），是流水线的最终输出。
- **`devShells`**：一个**临时、交互式**的 shell 环境。用 `nix develop` 进入后，`mlir-opt`、`circt-opt`、`yosys`、`torch-mlir-opt` 等命令都会出现在 `PATH` 里，方便你手动跑某一步降级、调试脚本。

devShell 不是构建产物，它更像一个「把全套工具装好的一次性实验室」。它和 `packages` 共享同一批被 pin 的工具，所以你在 devShell 里手动做的实验，和 CI 里 `nix build` 出来的结果用的是同一套工具版本。

#### 4.2.2 核心流程

```
nix develop  ──►  进入 devShells.default
                      │
                      ├── packages 列表里的工具全部进入 PATH
                      │     （mlir, circt, yosysPkg, torchMlir, ...）
                      │
                      └── shellHook 设置一批环境变量
                            （NEXTPNR_XILINX_DIR, PRJXRAY_DB_DIR, PYTHONPATH ...）
                      │
                      ▼
                  你可以在 shell 里手动执行降级/综合命令
```

#### 4.2.3 源码精读

```nix
devShells.default = pkgs.mkShell {
  packages = [
    mlir
    circt
    yosysPkg
    torchMlir
    llvmPackages.clang
    llvmPackages.llvm
    pythonWithTorch
    yosysSlang
    openXC7Nextpnr
    openXC7Prjxray
    openXC7Fasm
    prjxrayPythonDeps
    pkgs.cmake
    pkgs.ninja
    pkgs.gtkwave
    pkgs.nixfmt-classic
    pkgs.rr
  ];
  shellHook = ''
    export NEXTPNR_XILINX_DIR="${openXC7Nextpnr}/share/nextpnr"
    ...
    export PYTHONPATH="${prjxrayPythonPath}''${PYTHONPATH:+:$PYTHONPATH}"
  '';
};
```

[flake.nix:L761-L788](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L761-L788) —— 这就是 devShell 的定义。

- `packages` 列表：进入 shell 后这些工具都在 `PATH` 上。注意它们都是前面 inputs + outputs 里 pin/编译出来的同一套（`circt`、`yosysPkg`、`torchMlir`、`yosysSlang`、`openXC7Nextpnr`…），所以版本和 `nix build` 完全一致。
- `shellHook`：进入 shell 时执行的一段脚本，主要用来导出环境变量。比如 `NEXTPNR_XILINX_DIR` 告诉 nextpnr 去哪里找芯片数据库，`PRJXRAY_DB_DIR` 告诉 prjxray 工具数据库在哪，`PYTHONPATH` 把 prjxray 的 Python 依赖接上。这些变量是后续综合/比特流脚本正确运行的前提。

> 小提示：devShell 里既有「重型编译器」（clang、llvm、cmake、ninja），也有「调试/观察工具」（`gtkwave` 看波形、`rr` 做录制回放调试），还有 `nixfmt-classic` 用来格式化 nix 代码。

#### 4.2.4 代码实践

**实践目标**：确认 devShell 默认提供了哪些关键工具，并理解它们各自服务降级链的哪一段。

**操作步骤**：

1. 打开 [flake.nix:L761-L780](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L761-L780) 的 `packages` 列表。
2. 把列表里的工具按降级链分段归类：

   | 降级链阶段 | 对应 devShell 里的工具 |
   |-----------|----------------------|
   | MLIR 工具（通用） | `mlir` |
   | CIRCT 降级 | `circt` |
   | torch-MLIR 前端 | `torchMlir` |
   | SystemVerilog 综合 | `yosysPkg`、`yosysSlang` |
   | 比特流生成 | `openXC7Nextpnr`、`openXC7Prjxray`、`openXC7Fasm` |
   | 仿真/观察 | `gtkwave`（+ 仓库另用 `verilator`） |

3. 如果本地装了 Nix，执行 `nix develop`（或 `nix develop .#default`）进入 shell，然后运行 `which circt-opt yosys torch-mlir-opt`，观察它们的路径是否都在 `/nix/store/...` 下。

**需要观察的现象**：所有工具的可执行文件路径都形如 `/nix/store/<哈希>-<名字>/bin/...`，证明它们来自 Nix store，而不是系统自带。

**预期结果**：第 2 步能列出至少 5 个关键工具（如 `circt`、`torchMlir`、`yosysPkg`、`yosysSlang`、`openXC7Nextpnr`）。如果你没有 Nix 环境、无法运行 `nix develop`，第 3 步标注「待本地验证」即可——第 1、2 步纯靠读源码就能完成。

#### 4.2.5 小练习与答案

**练习 1**：devShell 里为什么要把 `yosysSlang` 单独列出来，它和 `yosysPkg` 是什么关系？

**参考答案**：`yosysPkg` 是 Yosys 主程序；`yosysSlang` 是一个 Yosys **插件**（`.so` 文件），给 Yosys 加上用 slang 读 SystemVerilog 的能力。综合阶段（u5-l1）会以 `yosys -m .../slang.so` 的方式加载它。两者必须版本匹配，所以它们在 flake 里是配套 pin、配套构建的（见 [flake.nix:L68-L105](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L68-L105) 的 `yosysSlang` 派生，构建时指向当前 `yosysPkg` 的 `yosys-config`）。

**练习 2**：`shellHook` 里如果不导出 `PRJXRAY_DB_DIR`，后续哪一步会出问题？

**参考答案**：生成比特流阶段（u5-l2 的 `mkBitstream`）要用 prjxray 数据库把 FASM 转成帧、再生成 `.bit`。没有 `PRJXRAY_DB_DIR`，`fasm2frames` / `xc7frames2bit` 会找不到芯片数据库而失败。

---

### 4.3 LLVM/torch-MLIR 的独立 pin 策略

这是本讲最微妙、也最值得反复看的一处设计，也是本讲的代码实践任务所在。

#### 4.3.1 概念说明

先说一个背景：**torch-MLIR 和 CIRCT 都依赖 LLVM/MLIR**，而且它们各自需要的 LLVM 版本不同、来源不同。如果粗暴地让整条工具链共用同一份 LLVM，会带来两个麻烦：

1. **版本冲突**：CIRCT 要的 LLVM 和 torch-MLIR 要的 LLVM 可能不是同一个 commit。
2. **重编译地狱**：LLVM 是个庞然大物，从源码编译要很久。torch-MLIR 是按 git submodule 方式带着自己的 LLVM 源码一起拉下来的。如果 torch-MLIR 用的 LLVM 和别处共用，那么你只要一改 torch-MLIR 的源码，就可能触发 LLVM 整体重编。

所以本项目采取的策略是：**给 torch-MLIR 单独 pin 一份 LLVM**，让它和别处（CIRCT 用的、devShell 通用工具用的）的 LLVM **互相独立**。

#### 4.3.2 核心流程

项目里其实存在「三套」LLVM/MLIR，各管一摊：

```
┌─────────────────────────────────────────────────────────────┐
│ ① 通用工具 LLVM：pkgsLlvm21.llvmPackages_21  (稳定 LLVM 21)  │
│    → 产出 mlir（给通用 mlir-opt）、clang、llvm，进 devShell   │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│ ② CIRCT 用的 LLVM：circt-nix 的 llvm-submodule-src           │
│    rev = 972cd847ef...  → 只用来编译 CIRCT                    │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│ ③ torch-MLIR 用的 LLVM：torchMlirLlvmPackages               │
│    rev = 3ca2a5fc... (LLVM 23.0.0-unstable)                  │
│    → 产出 mlirForTorchMlir、tblgen、llvm，喂给 torch-mlir.nix │
│    ★ 独立 pin，改 torch-mlir 源码不会重编这份 LLVM            │
└─────────────────────────────────────────────────────────────┘
```

模块 4.1 里你已经见过 ②（[flake.nix:L25-L31](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L25-L31)）。本模块重点看 ③，以及它和 ① 的分界。

#### 4.3.3 源码精读

**先看通用那套 LLVM（①）**：

```nix
llvmPackages = pkgsLlvm21.llvmPackages_21;
```

[flake.nix:L106](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L106) —— 来自前面 pin 好的 `nixpkgs-llvm21`，是稳定的 LLVM 21。后面 `inherit (llvmPackages) mlir;`（[flake.nix:L136](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L136)）取出的 `mlir` 就是这套，提供给 devShell 里通用的 `mlir-opt`。

**再看 torch-MLIR 专用的那套（③）**，这是本讲的核心：

```nix
# Keep LLVM for torch-mlir separate and pinned to torch-mlir's
# submodule revision so source edits in torch-mlir do not rebuild LLVM.
torchMlirLlvmPackages = (pkgsLlvm21.llvmPackages_git.override {
  llvmVersions = {
    "22.0.0-git" = {
      gitRelease = {
        rev = "3ca2a5fc0b84762f0e7d8a0e613fd69f7e344219";
        rev-version = "23.0.0-unstable-2026-01-20";
        sha256 = "sha256-jjdb2PtKnjYo9RIGJ82YtKmZinqEOlmm7R64SeJqTac=";
      };
    };
  };
}).overrideScope ...
```

[flake.nix:L107-L135](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L107-L135) —— 注意最上面那两行注释，**这就是本讲实践任务要找的那句注释**：

> Keep LLVM for torch-mlir separate and pinned to torch-mlir's submodule revision **so source edits in torch-mlir do not rebuild LLVM**.

翻译过来：把 torch-mlir 用的 LLVM 单独 pin 到 **torch-mlir 自己 submodule 里带的那个 LLVM 版本**，这样当你修改 torch-mlir 源码时，不会被迫重新编译 LLVM。

为什么是「torch-mlir 自己 submodule 里的版本」？因为 torch-mlir 是连着 submodule 一起拉下来的：

```nix
torchMlirSrc ? fetchFromGitHub {
  owner = "llvm";
  repo = "torch-mlir";
  rev = "59c249e5cc2025acca81bdcf1596b8dd36a5c0f9";
  fetchSubmodules = true;     # ← 连 submodule（含它自己那份 LLVM）一起拉
  hash = "sha256-o1HG5JuKRMEnl2PrEu5KQi4iqBe0Doh1SET2W/OjGoI=";
}
```

[torch-mlir.nix:L2-L8](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/torch-mlir.nix#L2-L8) —— torch-mlir 在 commit `59c249e5...` 处被拉取，且 `fetchSubmodules = true`。这意味着 torch-mlir 源码树里已经「声明」了它期望的 LLVM 版本。Nix 这边的 `torchMlirLlvmPackages` 就是要 **匹配这个 submodule 声明的版本**（这里 pin 到 LLVM `23.0.0-unstable`），从而让 Nix 构建出的 MLIR/LLVM 与 torch-mlir 源码期望的一致。

**最后，把这套 LLVM 喂给 torch-mlir 构建**：

```nix
torchMlir = pkgsLlvm21.callPackage ./torch-mlir.nix {
  inherit python;
  nanobind = nanobindBootstrap;
  inherit (torchMlirLlvmPackages) tblgen;   # ← 来自 ③ 那套 LLVM
  mlir = mlirForTorchMlir;                   # ← 由 ③ 那套 LLVM 构建
  inherit (torchMlirLlvmPackages) llvm;      # ← 来自 ③ 那套 LLVM
};
```

[flake.nix:L190-L196](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L190-L196) —— torch-mlir 的 `tblgen`、`mlir`、`llvm` 全部来自 `torchMlirLlvmPackages`（③），而不是通用的 `llvmPackages`（①）。这就实现了「独立 pin」：torch-mlir 这条线和 CIRCT 那条线、通用工具那条线彼此隔离，互不牵连。

其中的 `mlirForTorchMlir`（[flake.nix:L145-L165](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L145-L165)）也是基于 `torchMlirLlvmPackages.mlir` 再 override 出来的、带 Python 绑定的 MLIR，专供 torch-mlir 使用。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：找到「为什么 torch-MLIR 的 LLVM 要单独 pin」的注释，用自己的话复述原因；并从 devShell 列出 5 个关键工具。这正是本讲规格里规定的实践任务。

**操作步骤**：

1. **找注释**：打开 [flake.nix:L107-L108](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L107-L108)，找到以 `# Keep LLVM for torch-mlir separate...` 开头的两行注释。
2. **复述原因**：用一句话回答「为什么 torch-MLIR 的 LLVM 要单独 pin」。要点应包含：
   - torch-mlir 是带着 submodule（含自己的 LLVM）一起拉源码的；
   - 把 LLVM 单独 pin 到这个 submodule 版本；
   - 好处是改 torch-mlir 源码时不用重编 LLVM。
3. **列工具**：打开 [devShell 的 packages 列表](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L761-L780)，列出 5 个关键工具并各写一句作用。参考答案见下方 4.3.5。

**需要观察的现象**：注释紧贴在 `torchMlirLlvmPackages = ...` 这一行上方，说明这段 pin 的存在就是为了服务 torch-mlir，而非 CIRCT。

**预期结果**：
- 注释复述：「为了让 torch-mlir 的 LLVM 与它 submodule 里自带的版本一致，且与 CIRCT/通用工具的 LLVM 隔离，从而在修改 torch-mlir 源码时不必重新编译 LLVM。」
- 5 个关键工具示例：`circt`（CIRCT 降级）、`torchMlir`（torch-MLIR 前端）、`yosysPkg`（综合主程序）、`yosysSlang`（SystemVerilog 前端插件）、`openXC7Nextpnr`（布局布线）。

#### 4.3.5 小练习与答案

**练习 1**：项目里有几套 LLVM？分别服务谁？

**参考答案**：三套。① `llvmPackages`（`llvmPackages_21`，稳定 LLVM 21）服务通用 `mlir-opt` 和 devShell 里的 clang/llvm；② circt-nix 的 `llvm-submodule-src`（rev `972cd847ef`）只服务 CIRCT；③ `torchMlirLlvmPackages`（rev `3ca2a5fc`，LLVM 23.0.0-unstable）服务 torch-mlir。三者版本不同、彼此独立。

**练习 2**：如果有人把 `torchMlir` 的 `mlir = mlirForTorchMlir;` 改成 `mlir = mlir;`（即改用通用那套 ① 的 mlir），会有什么隐患？

**参考答案**：torch-mlir 源码（按 submodule）期望的是 LLVM 23 那套 MLIR 的接口/ABI，而通用 `mlir` 来自稳定的 LLVM 21，二者版本不一致，轻则编译 torch-mlir 时找不到符号/接口不匹配，重则产生隐蔽的运行期不兼容。这正是要「独立 pin」的根本原因。

**练习 3**：为什么注释强调「source edits in torch-mlir do not rebuild LLVM」？重编 LLVM 为什么是个问题？

**参考答案**：LLVM 是超大型 C++ 项目，完整编译可能要几十分钟到数小时。如果 torch-mlir 用的 LLVM 和某个「会被源码改动影响的派生」共用，那么每次改 torch-mlir 源码都可能让 Nix 认为 LLVM 派生失效、从而重编 LLVM，严重拖慢迭代。单独 pin 一份独立 LLVM 后，改 torch-mlir 源码只会重编 torch-mlir 本身，LLVM 派生保持命中缓存。

---

## 5. 综合实践

**任务**：从「一个 input」追踪到「devShell 里的一条命令」，把本讲三个模块串起来。我们选 CIRCT 作为追踪对象。

**目标**：亲手画出 CIRCT 这一个工具从「被声明为 input」到「出现在 devShell 的 PATH 上」的完整链路，验证「整条工具链都被钉死、且 devShell 与 nix build 共用同一套版本」这一结论。

**操作步骤**：

1. **input 层**：在 [flake.nix inputs](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L4-L44) 里找到两个与 CIRCT 相关的 input：`circt-src`（[L15-L21](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L15-L21)，fork 的 task3 分支）和 `circt-nix`（[L22-L32](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L22-L32)，构建逻辑 + 一个 pin 好的 LLVM submodule）。
2. **派生层**：找到 `circtBase` / `circt` 的定义（[L53-L59](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L53-L59)），确认它用的是上面 `circtPkgs`（即 circt-nix 提供、且 `circt-src` follow 过来的 fork）。
3. **devShell 层**：确认 `circt` 出现在 devShell 的 packages 列表里（[L763](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L763)）。
4. **画图**：把上面三层画成一条链：
   `circt-src(task3 fork) + circt-nix(LLVM pin 972cd847) → circtBase/circt 派生 → devShell 的 circt → PATH 上的 circt-opt`
5. **对比 LLVM**：在同一条链上标注「CIRCT 用的 LLVM 是 972cd847」，并对照模块 4.3 指出「torch-MLIR 用的是另一份 LLVM（3ca2a5fc）」，两者不共用。
6. （可选，需本地 Nix）执行 `nix develop`，运行 `circt-opt --version`，确认它确实来自 `/nix/store/...`。若无法运行，标注「待本地验证」。

**需要观察的现象**：CIRCT 的「源码版本」「LLVM 版本」「编译选项」三层都被 pin 死，且 devShell 与流水线产物共用同一个 `circt` 派生。

**预期结果**：你能用一句话说清——「我在 devShell 里敲的每一条 `circt-opt` 命令，背后都是被 flake.lock 钉死的 task3 fork + 精确 LLVM 编译出来的同一份 CIRCT，和 CI 里 `nix build` 用的完全一样。」

## 6. 本讲小结

- LLM2FPGA 之所以用 **Nix flake** 而不是 `pip`/`conda`，是因为它的工具链是多个版本高度敏感的 C++ 编译器（torch-MLIR、CIRCT、Yosys…），必须从精确 commit 重建，pip/conda 管不了。
- flake 的 **`inputs` 块**把每个工具钉死在具体 commit/分支上；`flake = false` 表示「只要源码、自己编译」，`follows` 表示「强制上游用我指定的那份依赖」。
- **`devShells.default`** 用 `nix develop` 进入，提供全套工具（circt、torchMlir、yosys、yosys-slang、openXC7 系列…）并通过 `shellHook` 配好环境变量，且这些工具与 `nix build` 产物共用同一套被 pin 的版本。
- 项目里存在 **三套互相独立的 LLVM/MLIR**：通用稳定 LLVM 21、CIRCT 用的 LLVM（972cd847）、torch-MLIR 用的 LLVM（3ca2a5fc / 23.0.0-unstable）。
- 关键注释（[flake.nix:L107-L108](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L107-L108)）解释了为什么要这样：**把 torch-mlir 的 LLVM 单独 pin 到它 submodule 的版本，避免改 torch-mlir 源码时重编 LLVM**。
- flake 还通过 `checks`（Nix/Python/SystemVerilog/Shell 静态检查）和 `apps.docs-md`（Org→Markdown）守住了质量与文档的一致性——这些会在 u7-l2 详讲。

## 7. 下一步学习建议

下一讲 **u1-l4「跑通第一个构建命令」** 会把本讲的环境真正用起来：执行 README 里的 gate 命令 `nix build .#tiny-stories-1m-baseline-float-selftest-all-memory-utilization`，并解读 `result/summary.txt` 里的 `clb_luts` 等关键字段。建议你：

- 先确保能在本机或 CI 上跑通 `nix develop`（验证本讲的工具集合）。
- 阅读 [README.md:L75](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L75) 那条 `nix build` 命令，试着把目标名拆成 `tiny-stories-1m / baseline-float / selftest / all-memory / utilization` 几段，猜猜每段含义（答案在 u1-l4）。
- 进阶后，可以回看 `outputs` 里 `packages`、`checks`、`apps` 三块，它们分别对应「产物 / 质量门禁 / 文档生成」，是后续 u3、u5、u7 多次引用的入口。
