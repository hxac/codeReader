# 构建系统、configure 与工具链

## 1. 本讲目标

上一讲我们建立了 Vortex 的「目录地图」，知道 `hw`、`sw`、`sim`、`tests` 各自装着什么。但你一定会发现一个反直觉的现象：仓库根目录里那些 `.in` 后缀的文件（`Makefile.in`、`config.mk.in`、`ci/toolchain_install.sh.in`……）**并不是最终被构建系统直接使用的文件**。真正驱动构建的，是一棵由 `configure` 脚本「生成」出来的、可运行的树。

本讲的目标是让你搞清楚这棵树是怎么长出来的：

- 掌握 **out-of-tree（源外构建）** 的含义：为什么构建产物不进源码树，而是进一个独立的 `build/` 目录。
- 读懂 `configure` 脚本：它如何通过 `sed` 把工具链绝对路径、`XLEN`、`VORTEX_HOME` 等「变量」**烘焙（bake）** 进生成的 `config.mk` / 各 `Makefile` / `ci/*.sh`。
- 理解工具链安装脚本 `ci/toolchain_install.sh` 如何把预编译好的 Verilator、RISC-V GNU 工具链、LLVM 等下载到 `$TOOLDIR`。
- 牢记 AGENTS.md 里那条**不可违反的核心纪律**：「改了 toml / Makefile，必须重新 `configure`」，以及忘记它会踩到「stale `VX_config.h`」这个真实大坑。
- 学会解释**为什么一台机器上能同时存在多棵配置不同的 Vortex 树**（`build32/`、`build64/`、`build_fpga64/`……）而互不污染。

学完后，你应该能独立完成 Vortex 的首次配置（`../configure` + `toolchain_install.sh` + `make`），并在遇到「我改了 Makefile 却没生效」时，第一反应是「我忘记重新 configure 了」。

## 2. 前置知识

阅读本讲前，你应该已经学过：

- **u1-l1《Vortex 是什么》**：知道 Vortex 是全栈 GPGPU，有 simx / rtlsim / opae / xrt 多种后端，由 `$VORTEX_DRIVER` 切换。
- **u1-l2《仓库目录结构地图》**：知道 `hw`、`sw`、`sim`、`tests`、`ci`、`docs` 等一级目录的职责，以及根目录的 `VX_config.toml` / `VX_types.toml` 是硬件配置的「单一真相来源」。

如果下面几个名词对你还陌生，先看一句话解释：

- **out-of-tree build（源外构建）**：把「源码」和「构建产物」放在两个不同目录。你 `git clone` 下来的是源码树（只读），你在它旁边新建一个 `build/` 目录存放所有生成的文件和中间产物。Linux 内核、LLVM 等大型项目都采用这种模式。
- **`.in` 模板文件**：带 `.in` 后缀的文件是「模板」，里面含 `@占位符@`。构建系统会把占位符替换成真实值，去掉 `.in` 后缀，得到最终文件。例如 `config.mk.in` → `config.mk`、`ci/blackbox.sh.in` → `ci/blackbox.sh`。
- **XLEN**：RISC-V 的位宽，Vortex 支持 `--xlen=32` 和 `--xlen=64`。它决定了用哪套 RISC-V 工具链（`riscv32-` 还是 `riscv64-`）和哪种页表格式。
- **TOOLDIR**：一个本地目录（默认 `$HOME/tools`），Vortex 把所有第三方工具（Verilator、RISC-V 工具链、LLVM……）都装在这里。

> 一个贯穿全讲的直觉：**`configure` 的本质就是一台「模板填空机」**——它读取几个参数（XLEN、TOOLDIR……），然后把它们的值填进成百上千个 `.in` 模板，复制到 `build/` 里，造出一棵「插电就能用」的 Vortex 树。

## 3. 本讲源码地图

本讲涉及的关键文件分为三类：**配置脚本**、**模板文件**、**纪律文档**。

| 文件 | 类型 | 作用 |
| --- | --- | --- |
| [configure](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure) | 配置脚本 | 全栈构建的「总开关」。解析参数、烘焙工具路径、生成可运行树。 |
| [config.mk.in](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/config.mk.in) | 模板 | 全局 `config.mk` 的模板，定义 `VORTEX_HOME`、`VERILATOR_PATH`、RISC-V 工具链路径等所有 Make 都会 `include` 的变量。 |
| [Makefile.in](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/Makefile.in) | 模板 | 顶层 `Makefile` 的模板，定义 `all` / `hardware` / `software` / `tests` / `install` 等目标。 |
| [ci/toolchain_install.sh.in](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/toolchain_install.sh.in) | 模板 | 工具链安装脚本模板：从 GitHub 拉取预编译的 Verilator / RISC-V / LLVM / PoCL 等，解压到 `$TOOLDIR`。 |
| [VERSION](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VERSION) | 版本钉 | 定义 `VORTEX_VERSION` / `TOOLCHAIN_REV` / `GEM5_REV`，被 `configure` 与安装脚本共同 `source`。 |
| [AGENTS.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md) | 纪律文档 | 第 2 节「Build & Toolchain Rules」列出构建的不可违反规则。 |

此外，我们会用两个真实的子目录 `common.mk`（[sim/common.mk](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common.mk)、[sw/runtime/common.mk](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common.mk)）来验证「生成的 `config.mk` 如何被层层 `include`」。

> 说明：本讲引用的文件与行号均在当前 HEAD（`d76b7f24e`）下实际确认。

## 4. 核心概念与源码讲解

### 4.1 configure 脚本：生成一棵可运行的树

#### 4.1.1 概念说明

很多项目的 `configure` 是 `autoconf` 生成的几千行怪物脚本，但 Vortex 的 `configure` 是一份**手写的、约 240 行的 bash 脚本**，逻辑非常清晰，完全可以通读。理解它的关键是一个设计决策：

> **源码树（source tree）是只读的、共享的；构建树（build tree）是生成的、一次性的。**

你永远不会在源码树里 `make`，而是在源码树旁边建一个 `build/` 目录，进到里面运行 `../configure`。`configure` 跑完后，`build/` 里就长出了一棵「插电就能用」的 Vortex 树——有顶层的 `Makefile`、各子目录的 `Makefile`、可直接执行的 `ci/blackbox.sh`、`ci/toolchain_install.sh`，以及由 toml 生成的 `hw/*.vh` 和 `sw/*.h` 头文件。

这就是 [docs/install_vortex.md:L27-L33](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/install_vortex.md#L27-L33) 描述的标准首次构建流程：

```bash
$ cd vortex
$ mkdir -p build
$ cd build
$ ../configure --xlen=32 --tooldir=$HOME/tools
$ ./ci/toolchain_install.sh
$ make -s
```

注意第 4 行：`configure` 是用 **`../configure`**（相对路径指向源码树根）调用的，而当前工作目录是 `build/`。这一点决定了 `configure` 内部如何区分「源」与「目标」。

#### 4.1.2 核心流程

`configure` 的执行可以拆成 5 个阶段。用伪代码描述：

```
阶段 0：确定「我在哪、源在哪」
    CURRENT_DIR = pwd                      # 通常是 build/
    SOURCE_DIR  = configure 脚本自身的目录  # 源码树根
    若 CURRENT_DIR == SOURCE_DIR（在源码树里直接 configure），则原地生成

阶段 1：确定配置参数
    default_xlen      = 32
    default_tooldir   = $HOME/tools
    default_osversion = detect_osversion()   # 读 /etc/os-release
    default_prefix    = CURRENT_DIR/install
    # 先尝试从已存在的 build/config.mk 读取旧值作为新默认值（增量配置友好）
    再用命令行 --xlen / --tooldir / --osversion / --prefix 覆盖

阶段 2：盖一个「配置戳记」.config.stamp
    CONFIG_SIG = "XLEN=... TOOLDIR=... OSVERSION=... PREFIX=... SOURCE_DIR=... BUILDDIR=..."
    把 CONFIG_SIG 写入 build/.config.stamp
    （这个戳记是后续判断「是否需要重新生成某个文件」的时间戳基准）

阶段 3：复制 + 实例化 → 生成可运行树
    遍历 SUBDIRS = (".", "!ci", "!perf", "hw*", "sw*", "sim*", "tests*")
      对每个源码子目录：
        - 带 ! 前缀的（ci、perf）：整个目录全量复制
        - 其余（hw/sw/sim/tests 及根目录）：只复制 Makefile / common.mk / *.in
      对每个 .in 文件：用 sed 把 @占位符@ 替换成真实值，去掉 .in 后缀写出
        （关键占位符：@VORTEX_HOME@ @XLEN@ @TOOLDIR@ @OSVERSION@
                     @INSTALLDIR@ @BUILDDIR@ @TOOLCHAIN_REV@ @VORTEX_VERSION@）

阶段 4：从 toml 生成头文件
    对仓库根的每个 *.toml：
        gen_config.py --config <toml> --output build/hw/<name>.vh --format verilog
        gen_config.py --config <toml> --output build/sw/<name>.h  --format cpp
    （VX_types.toml 额外加 --resolved，因为它的值依赖 XLEN）
```

其中**阶段 3 的 `sed` 替换**是整个机制的灵魂——它把绝对路径和参数「焊死」到生成的文件里。这也是「为什么多棵 Vortex 树能共存」的根源：每棵树的 `config.mk` 里都带着自己的、互不相同的绝对路径。

#### 4.1.3 源码精读

**阶段 0——区分源与目标。** `configure` 一开头就抓取当前工作目录作为构建目录：

[configure:L16-L17](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L16-L17) —— 记录 `CURRENT_DIR=$(pwd)`，这就是 `build/`（或你所在的任意目标目录）。

稍后，脚本通过 `BASH_SOURCE` 反推自己所在的源码树根：

[configure:L188-L189](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L188-L189) —— `SOURCE_DIR` 取 `configure` 脚本自身的目录，即源码树根。`CURRENT_DIR` 与 `SOURCE_DIR` 这一对，正是 out-of-tree 构建的「目标 / 源」二元组。

**阶段 1——默认参数与增量配置。** 默认值集中定义在一段：

[configure:L131-L136](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L131-L136) —— 5 个默认参数：`default_xlen=32`、`default_tooldir=$HOME/tools`、`default_osversion` 由 `detect_osversion` 探测、`default_prefix=$CURRENT_DIR/install`。

一个对「重复 configure 很友好」的细节是它会**从已存在的 `build/config.mk` 里读取上次的值**作为新的默认值：

[configure:L137-L150](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L137-L150) —— 若当前目录已有 `config.mk`，就把里面的 `XLEN / TOOLDIR / OSVERSION / PREFIX` 读出来当作新默认值。这样你第二次跑 `../configure`（不带参数）时，会沿用上次的配置，而不是退回 `xlen=32`。

随后命令行参数覆盖默认值，可用选项就 4 个：

[configure:L159-L177](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L159-L177) —— `--xlen=` / `--tooldir=` / `--osversion=` / `--prefix=`（`--installdir=` 是它的别名），外加 `-h/--help`。

**阶段 2——配置戳记。** 这是后续「增量生成」的时间戳基准：

[configure:L196-L201](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L196-L201) —— 把 5 个参数拼成 `CONFIG_SIG` 写入 `build/.config.stamp`。只要参数没变，这个戳记的内容就不变；参数一变，戳记的 mtime 就会更新，从而触发 `.in` 文件和 toml 头文件重新生成。

**阶段 3——复制与实例化。** 这是 `configure` 的主调用：

[configure:L203](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L203) —— `copy_files "$SOURCE_DIR" "$CURRENT_DIR"` 把源码树「投影」到构建树。

要复制哪些目录由这个数组决定：

[configure:L185-L186](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L185-L186) —— `SUBDIRS=("." "!ci" "!perf" "hw*" "sw*" "sim*" "tests*")`。`!` 前缀表示「全量复制」（ci、perf 的所有文件都拷过去），其余表示「只复制 Makefile / common.mk / *.in」。根目录 `"."` 负责生成顶层的 `Makefile`（来自 `Makefile.in`）和 `config.mk`（来自 `config.mk.in`）。

> 这就解释了为什么 `build/ci/` 里有可直接执行的 `blackbox.sh`、`toolchain_install.sh`、`regression.sh`——它们都是 `!ci` 全量复制时，从对应的 `.sh.in` 实例化而来的。而 `hw/`、`sw/`、`sim/`、`tests/` 下的大量源码文件（`.sv`、`.cpp`、`.h`）**并不会被复制**，它们仍然待在源码树里，构建时通过 `VORTEX_HOME`（= `SOURCE_DIR`）按原路径引用。

那台「模板填空机」的核心是一条 `sed` 命令：

[configure:L70-L85](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L70-L85) —— 对每个 `.in` 文件，用 `sed` 把 `@VORTEX_HOME@`、`@XLEN@`、`@TOOLDIR@`、`@OSVERSION@`、`@INSTALLDIR@`、`@BUILDDIR@`、`@TOOLCHAIN_REV@`、`@VORTEX_VERSION@`、`@GEM5_REV@` 这 9 个占位符全部替换成真实值，去掉 `.in` 后缀写到目标目录；并且如果生成的是 bash 脚本（首行 `#!...bash`），自动加上可执行权限。注意 L76 还有一个**增量优化**：若目标文件比源 `.in` 和 `.config.stamp` 都新，就跳过——这是 `configure` 可以反复安全运行的保障。

**阶段 4——从 toml 生成头文件。** 这是配置系统的「值流」入口（详细机理在 U2 展开，本讲只看 `configure` 如何触发）：

[configure:L228-L237](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L228-L237) —— 对仓库根的每个 `*.toml`，调用 `ci/gen_config.py` 生成两份产物：`build/hw/<name>.vh`（Verilog 宏，`--format verilog`）和 `build/sw/<name>.h`（C++ 宏，`--format cpp`）。其中 `VX_types.toml` 额外加 `--resolved`，因为它的一些值（如 `[memmap]`、`[vm]`）依赖 `XLEN`，需要 `gen_config.py` 结合环境变量 `XLEN` 求值（见上一行 `export XLEN`）。

> `gen_config.py` 的三种输出格式（`cflags` / `cpp` / `verilog`）正是配置值流向 RTL、运行时、内核的三个出口。它的 CLI 在 [ci/gen_config.py:L1485-L1490](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1485-L1490) 定义。本讲只需记住：`configure` 调它生成 `vh`/`h`；后续 `common.mk` 还会调它生成 `cflags`。完整值流图留到 U2。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次 `configure`，打开生成的 `config.mk`，看见绝对路径是如何被「烘焙」进去的，并据此解释多棵树共存的原理。

**操作步骤**：

1. 在源码树旁建一个构建目录并进入：

   ```bash
   mkdir -p build && cd build
   ```

2. 用 `--xlen=64` 调用 configure（先只看生成结果，不安装工具链也能做）：

   ```bash
   ../configure --xlen=64
   ```

3. 打开生成的顶层 `config.mk`，定位这几行：

   ```bash
   grep -nE 'VORTEX_HOME|VERILATOR_PATH|RISCV_TOOLCHAIN_PATH|^XLEN' config.mk
   ```

4. 再随便挑一个被全量复制的脚本，对比模板与产物：

   ```bash
   diff <(grep '@TOOLDIR@\|@XLEN@\|@OSVERSION@' ../ci/toolchain_install.sh.in) \
        <(grep -E 'TOOLDIR=|OSVERSION=' ci/toolchain_install.sh | head)
   ```

**需要观察的现象**：

- `config.mk` 里的 `XLEN ?= 64`（不再是模板里的 `@XLEN@`）、`VORTEX_HOME ?= /绝对/路径/到/源码树`、`VERILATOR_PATH ?= $HOME/tools` 形如 `/home/你/tools` 的绝对路径——所有 `@占位符@` 都已变成真实值。
- `ci/toolchain_install.sh` 是**可执行**的（`ls -l ci/toolchain_install.sh` 看到 `x` 位），而源码树里的 `ci/toolchain_install.sh.in` 不是。
- `build/hw/` 下出现 `VX_config.vh`、`VX_types.vh`；`build/sw/` 下出现 `VX_config.h`、`VX_types.h`。

**预期结果**：你会直观地看到「`configure` = 模板填空机」——同一份 `.in` 模板，填入不同的 `XLEN`/`TOOLDIR`，就生成不同的 `config.mk`。这直接回答了实践任务里的问题：**多棵 Vortex 树能共存，是因为每棵树的 `config.mk` 都自带互不相同的绝对路径与 `XLEN`，彼此不依赖任何全局环境变量。** 你可以同时有 `build32/`（`--xlen=32`）和 `build64/`（`--xlen=64`），它们各自指向 `riscv32-` 与 `riscv64-` 工具链，互不污染。

> 如果你没有联网或尚未 `toolchain_install.sh`，第 2、3 步仍可完成——`configure` 不要求工具链已存在，它只是把路径写进去；真正校验路径有效性的是后续 `make`。本实践为「配置生成型」，未实际执行 `make`，故构建能否跑通标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果你在 `build/` 里改了某个 `Makefile`（注意是 build 树里的，不是源码树里的 `.in`），然后重新跑 `../configure`，你的改动会保留吗？
**参考答案**：不会，会被覆盖（除非目标文件比源 `.in` 和 `.config.stamp` 都新，那它会被增量跳过；但只要 `.config.stamp` 更新了，就会重新生成）。这正是 AGENTS.md 强调「要改 Makefile 就改源码树里的 `.in`，再重新 configure」的原因——build 树里的 `Makefile` 是**生成物**，不是编辑对象。

**练习 2**：`SUBDIRS` 里 `ci` 和 `hw` 一个带 `!`、一个不带，行为有什么不同？为什么 `ci` 要全量复制？
**参考答案**：带 `!` 表示全量复制整个目录的所有文件；不带表示只复制 `Makefile` / `common.mk` / `*.in`。`ci` 需要全量复制，是因为它里面有大量非 `.in` 的脚本（`blackbox.sh.in` 实例化后的产物、`gen_config.py`、`conftest.py`、测试用例 yaml 等）都要进 build 树才能被 `./ci/xxx.sh` 直接调用；而 `hw`/`sw`/`sim`/`tests` 的源码体积庞大且不需要复制，只需生成对应的 `Makefile`，源文件通过 `VORTEX_HOME` 原地引用即可。

---

### 4.2 工具链与 config.mk：路径如何流到每一个 Makefile

#### 4.2.1 概念说明

阶段 1 讲了 `configure` 会把工具路径写进 `config.mk`。但一个 GPU 项目要用的工具非常多——Verilator（RTL 仿真）、RISC-V GNU 工具链（交叉编译设备代码）、LLVM（VOLT 编译器后端）、PoCL / chipStar / mesa（上层 API 桥接）……这些工具从哪来、装在哪、又怎么被成百上千个子目录的 Makefile 找到？

Vortex 的答案是两段式：

1. **`ci/toolchain_install.sh`** 负责把所有预编译工具下载、解压到统一的 `$TOOLDIR`（默认 `$HOME/tools`）。
2. **`config.mk`** 负责在 `$TOOLDIR` 下定义每一个工具的子路径，再被所有子目录 `include`。

这种「集中定义、层层 include」的结构，让你只要改一处（`configure` 的 `--tooldir`），全树的工具路径就跟着变。

#### 4.2.2 核心流程

工具链从下载到被编译器使用的数据流：

```
VERSION:  TOOLCHAIN_REV = v3.0          ──┐
                                          │  决定从 GitHub 哪个 tag 下载
toolchain_install.sh.in (--tooldir=$HOME/tools) ─→ wget + 解压到 $TOOLDIR/
      │                                      产生：$TOOLDIR/verilator/
      │                                            $TOOLDIR/riscv64-gnu-toolchain/
      │                                            $TOOLDIR/llvm-vortex/ ...
      ▼
configure 把 @TOOLDIR@ 烘焙进 config.mk
      │
      ▼
config.mk 定义：
      VERILATOR_PATH      = $(TOOLDIR)/verilator
      RISCV_TOOLCHAIN_PATH= $(TOOLDIR)/riscv$(XLEN)-gnu-toolchain
      LLVM_PATH           = $(TOOLDIR)/llvm-vortex
      ...
      │
      ▼
每个子目录的 common.mk：
      ROOT_DIR := $(realpath ../..)      # 回到 build 根
      include $(ROOT_DIR)/config.mk       # 拿到全部工具路径
      RTL_DIR := $(VORTEX_HOME)/hw/rtl    # 再定位源码
```

关键点：**`config.mk` 是全树唯一的「变量字典」**，每个 `common.mk` 第一步都是 `include` 它。于是工具路径只需要在一个地方（`config.mk.in` 模板）维护。

#### 4.2.3 源码精读

**版本钉——决定下载哪个 tag。** `configure` 和安装脚本都 `source` 同一份版本文件：

[VERSION:L1-L3](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VERSION#L1-L3) —— `VORTEX_VERSION=3.0`、`TOOLCHAIN_REV=v3.0`、`GEM5_REV=v25.0.0.1`。`TOOLCHAIN_REV` 就是预编译工具链在 GitHub 上的 git tag。

`configure` 在阶段 1 里 `source "$SOURCE_DIR/VERSION"`（[configure:L193-L194](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L193-L194)），从而拿到 `VORTEX_VERSION` 和 `TOOLCHAIN_REV` 去填占位符。

**下载与解压——`toolchain_install.sh`。** 安装脚本实例化后，`@TOOLDIR@`、`@TOOLCHAIN_REV@`、`@OSVERSION@` 都已变成真实值：

[ci/toolchain_install.sh.in:L19-L23](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/toolchain_install.sh.in#L19-L23) —— 仓库基址指向 `github.com/vortexgpgpu/vortex-toolchain-prebuilt/raw/${TOOLCHAIN_REV}`，即按 OS 版本和 tag 拉取预编译包。

每个工具是一个同名函数，模式一致：分段 `wget` → `cat` 拼合 → `tar -xvf` → 移到 `$TOOLDIR`。以 Verilator 为例：

[ci/toolchain_install.sh.in:L143-L159](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/toolchain_install.sh.in#L143-L159) —— `verilator()` 函数：按 OS 选分片数，逐片 `wget`，拼合成 `verilator.tar.bz2`，解压后 `mv verilator $TOOLDIR`。

不带参数时默认安装除 gem5 外的所有组件：

[ci/toolchain_install.sh.in:L217-L233](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/toolchain_install.sh.in#L217-L233) —— `install_base()` 依次安装 PoCL、chipStar、Verilator、LLVM、libcrt/libc（32 与 64）、RISC-V 工具链（32 与 64）、sv2v、yosys、sta、mesa。gem5 因体积大单独用 `--gem5` 显式安装。

**路径字典——`config.mk.in`。** 这是全树变量定义的模板：

[config.mk.in:L14-L27](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/config.mk.in#L14-L27) —— `VORTEX_VERSION`、`VORTEX_HOME`（源码树根）、`VERILATOR_PATH=$(TOOLDIR)/verilator`、`XLEN`、`TOOLDIR`、`OSVERSION`、`INSTALLDIR` 都在这定义。注意它们都用 `?=`（条件赋值），意味着命令行或环境变量可以覆盖，但默认值来自 `configure` 烘焙的 `@...@`。

RISC-V 工具链路径按 `XLEN` 自动选择：

[config.mk.in:L31-L39](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/config.mk.in#L31-L39) —— `LLVM_PATH=$(TOOLDIR)/llvm-vortex`、`RISCV_TOOLCHAIN_PATH=$(TOOLDIR)/riscv$(XLEN)-gnu-toolchain`、`RISCV_PREFIX=riscv$(XLEN)-unknown-elf`。这就是「`--xlen=64` 自动用 `riscv64-` 工具链」的来源——`$(XLEN)` 被代入路径与前缀。

**层层 include——`common.mk`。** 每个子目录的 `common.mk` 都先回到 build 根去 `include config.mk`：

[sim/common.mk:L1-L7](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common.mk#L1-L7) —— `ROOT_DIR := $(realpath ../..)` 计算出 build 根，`include $(ROOT_DIR)/config.mk` 拿到全部工具路径，再用 `$(VORTEX_HOME)` 定位源码（`HW_DIR`、`RTL_DIR`）。`sw/runtime/common.mk` 结构相同（[sw/runtime/common.mk:L1-L7](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common.mk#L1-L7)），但它多了一步——直接调 `gen_config.py` 把 toml 解析成 `-DVX_CFG_*` 编译宏（[sw/runtime/common.mk:L14](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common.mk#L14)），这样运行时代码不必 `#include <VX_config.h>` 也能拿到配置。

**顶层 Makefile 的目标骨架——`Makefile.in`。** 顶层 `Makefile` 第一步也是 `include config.mk`：

[Makefile.in:L1-L19](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/Makefile.in#L1-L19) —— `all` 依赖 `third_party → hardware → software → tests`；`hardware` 先 `make -C hw` 再 `make -C sim`；`software` 编 `sw/kernel` 与 `sw/runtime/stub`。这就是 `make -s` 时实际发生的事。

#### 4.2.4 代码实践

**实践目标**：顺着「`toolchain_install.sh` → `config.mk` → `common.mk` → 编译器」这条链，把工具路径「抓现行」，验证它确实是一路传递下来的。

**操作步骤**：

1.（若已装好工具链）确认 `$TOOLDIR` 下的工具子目录：

   ```bash
   ls $HOME/tools        # 应看到 verilator/ riscv64-gnu-toolchain/ llvm-vortex/ 等
   ```

2. 在 `build/` 里查看 `config.mk` 如何把 `TOOLDIR` 展开成各工具路径：

   ```bash
   cd build
   grep -nE 'TOOLDIR|VERILATOR_PATH|RISCV_TOOLCHAIN_PATH|LLVM_PATH|XLEN ' config.mk
   ```

3. 选一个子目录，展开它 `include` 后的工具路径（模拟 make 的视角）：

   ```bash
   make -C sim -n 2>/dev/null | grep -oE '\$\$(TOOLDIR|VORTEX_HOME)[^ ]*' | head
   # 或直接打印变量：
   make -C sim --no-builtin-rules --no-builtin-variables -p 2>/dev/null | grep -E '^(VERILATOR_PATH|RISCV_TOOLCHAIN_PATH|VORTEX_HOME|XLEN) :?='
   ```

**需要观察的现象**：

- `config.mk` 里 `TOOLDIR ?= /home/<你>/tools`，而 `VERILATOR_PATH ?= $(TOOLDIR)/verilator`、`RISCV_TOOLCHAIN_PATH ?= $(TOOLDIR)/riscv64-gnu-toolchain`（当你用 `--xlen=64` 时）。
- `make -p` 打印出的这些变量值，已是**完全展开的绝对路径**（如 `/home/<你>/tools/verilator`），证明子目录通过 `include config.mk` 确实拿到了烘焙好的路径。
- 如果你同时建了 `build32/`（`--xlen=32`）和 `build64/`（`--xlen=64`），两者的 `RISCV_TOOLCHAIN_PATH` 会分别指向 `riscv32-gnu-toolchain` 与 `riscv64-gnu-toolchain`——同一台机器、同一个 `$HOME/tools`、两套互不干扰的配置。

**预期结果**：你会确认「工具路径只在 `config.mk` 一处定义，靠 `include` 流到全树」这一结构。这也解释了 [docs/install_vortex.md:L38-L41](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/install_vortex.md#L38-L41) 所说的：`../configure` 把完整工具链布局写进 build 目录的 Makefiles，**无需 source 任何 shell 环境、也不会被全局 `~/.bashrc` 串扰**，因此多棵 Vortex 树可共存。

> 若本机尚未 `toolchain_install.sh`，第 1、3 步会因工具缺失而无输出，属正常；本实践的核心结论（路径传递结构）从第 2 步的 `config.mk` 内容即可得出。是否实际编译通过标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `config.mk.in` 里所有变量都用 `?=` 而不是 `=`？
**参考答案**：`?=` 是「仅当未定义时才赋值」。这样既给了 `configure` 烘焙的默认值，又允许命令行（如 `make XLEN=64`）或上层环境临时覆盖。若用 `=`，则每次都会被覆盖回默认值，丧失灵活性。

**练习 2**：`toolchain_install.sh` 为什么要把大文件切成 `parta`、`partb`…… 再 `cat` 拼合，而不是直接下载一个完整包？
**参考答案**：GitHub 对单文件有大小限制（通常 100 MB，且大文件在 raw 下载链路上不稳定）。Vortex 的预编译工具链（尤其 RISC-V 工具链）体积很大，故切成多片（如 `riscv64` 在 focal 上分 `a..j` 共 10 片，见 `toolchain_install.sh.in` L43-L58）绕过限制，下载端再 `cat *.parta*` 拼回完整 tar 包。

---

### 4.3 AGENTS.md 第 2 节：重新 configure 的纪律与「stale 头文件」大坑

#### 4.3.1 概念说明

理解了 `configure` 的工作原理，你就能理解为什么 AGENTS.md——Vortex 给人和 AI 协作者的「规则圣经」——把构建纪律放在如此突出的位置。第 2 节「Build & Toolchain Rules」用粗体强调了多条**non-negotiable（不可商量）**的规则。其中最关键、最容易踩的一条是：

> **凡是动了 toml / Makefile / 增删构建参与目录，都必须从 `build/` 重新 `../configure`。**

为什么这么严格？因为 `configure` 的产物（`config.mk`、各 `Makefile`、`hw/*.vh`、`sw/*.h`）都是**生成物**，而生成是**按 mtime 增量**的。一旦源（`.in`、`*.toml`）变了但 `configure` 没重跑，生成物就会「过期」却仍被使用，导致行为与真相来源不一致。

#### 4.3.2 核心流程

判断「是否需要重新 configure」的决策树：

```
你是否做了以下任一件事？
  ├── git pull（拉到新代码，Makefile/toml 可能已变）
  ├── 编辑了任何源码树里的 Makefile / common.mk / *.in
  ├── 编辑了 VX_config.toml / VX_types.toml / 任何 *.toml
  ├── 增删了一个「构建参与目录」
  └── 改了 --xlen / --tooldir 等参数
        │
        是 ──→ 从 build/ 重新跑 ../configure，再 make
        │       （configure 的 .config.stamp 会触发增量重生成本次受影响的文件）
        │
        否 ──→ 直接 make 即可（增量编译未变更的源）
```

特别地，`VX_config.toml` 的改动有一个隐蔽的失效模式，AGENTS.md 用一个**真实事故**来警示：simx/RTL 的核心代码会 `#include` 由 toml 生成的 `VX_config.h`。`configure` 虽然会在 toml 更新时重生 `VX_config.h`，但**只在该 toml 比生成头新时才重生**。如果时间戳判断出了岔子（或你绕过了 configure 直接改 Makefile），核心就会对着一份**过期的** `VX_config.h` 编译，与 toml/RTL 静默地不一致。

#### 4.3.3 源码精读

**out-of-tree 标准配方。** AGENTS.md 第 2 节开篇就给出不可商量的流程：

[AGENTS.md:L46-L52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L46-L52) —— 从 repo 根 `mkdir build && cd build`、`../configure --xlen=32 --tooldir=...`、首次运行 `./ci/toolchain_install.sh`、`make -s`。这正是本讲 4.1、4.2 两节拆解的那条命令链。

**一变体一棵树。** 强调不要复用一个 build 目录跑不兼容配置：

[AGENTS.md:L53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L53) —— 为每个主要变体（`build32/`、`build64/`、`build_fpga64/`……）建独立 build 目录，避免配置/工具污染。这正是 4.1 实践里「多棵树共存」的官方依据。

**configure 生成的是「可运行树」。** 解释了为什么测试和执行都应优先用 build 树里的脚本：

[AGENTS.md:L54](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L54) —— `configure` 通过复制并实例化 `ci/`、`runtime/`、`sim/`、`tests/` 生成一棵可运行树；执行和测试自动化时，**始终优先用 build 树里生成的脚本/Makefile**，而不是源码树里的 `.in` 文件。

**重新 configure 的纪律。** 这是本节的核心规则，列出了所有触发条件：

[AGENTS.md:L55](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L55) —— 凡 `git pull`、改 Makefile、改任何 `*.toml`、增删构建参与目录，都要重新 `../configure`。并点出忘记它的典型症状：「我改了这个 Makefile，结果什么都没发生」。

**stale `VX_config.h` 大坑。** 这是整篇 AGENTS.md 里最长的单条规则，用一个真实事故讲清后果与正确处置：

[AGENTS.md:L56](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L56) —— `configure` 只在 toml 比生成头新时才重生成 `build/sw/VX_config.h` 与 `build/hw/*.vh`；simx/RTL 核心 `#include` 这份生成头。一旦它过期，核心会按旧配置值编译，与 toml/RTL 静默背离。文中举了真实案例：一份过期 `VX_config.h` 曾让 SimX 跑成 write-back D-cache，而 toml/RTL 是 write-through，导致**只有 SimX 出错**。处置纪律是：**绝不能**靠往 Makefile 里塞 `-DVX_CFG_*` 覆盖来「绕过」这种背离——那只会掩盖过期产物、和配置系统对着干；正确做法是重新 `configure`，让 toml 这唯一真相来源重新生效。

> 这条规则把本讲的三块知识串了起来：toml 是真相来源（u1-l2 讲过）→ `configure` 把 toml 转成 `VX_config.h`（本讲 4.1）→ 核心 `#include` 它（本讲 4.2 的 include 文化）。漏掉中间这步 `configure`，整条链就断了。

#### 4.3.4 代码实践

**实践目标**：复现「改了 toml 却不重新 configure 会怎样」，从而内化「重新 configure」的纪律。

**操作步骤**：

1. 在 `build/` 里先确认当前 `VX_config.h` 里某个参数的值（例如 `NUM_THREADS`）：

   ```bash
   cd build
   grep -nE 'VX_CFG_NUM_THREADS' sw/VX_config.h
   ```

2. 走到**源码树**（不是 build 树），在 `VX_config.toml` 里找到 `num_threads`，记下当前值（**先不要改**）：

   ```bash
   grep -nE 'num_threads' ../VX_config.toml
   ```

3. 故意不重新 configure，直接 `make -s`，再次 `grep sw/VX_config.h`，观察其值是否变化。
4. 现在按纪律做：`cd build && ../configure`（重新生成头），再 `grep sw/VX_config.h` 看时间戳与内容是否刷新。
5.（可选，**务必在实验后改回原值**）把 toml 里的 `num_threads` 改一个值，重复第 3、4 步，对比「不 configure」与「重新 configure」两种情况下 `sw/VX_config.h` 是否更新。

**需要观察的现象**：

- 第 3 步：只 `make` 不 `configure` 时，`sw/VX_config.h` 通常不会因 toml 改动而更新（因为 `make` 本身不调 `gen_config.py` 重新生成顶层头；只有 `configure` 才会）。这正是 stale 风险的来源。
- 第 4 步：重新 `configure` 后，由于 toml 比 `VX_config.h` 新（或 `.config.stamp` 更新），`gen_config.py` 被触发，`VX_config.h` 内容/时间戳刷新。
- 第 5 步：改动 toml 但不 configure → `VX_config.h` 仍是旧值 → 核心 `#include` 到旧值 → 这就是 AGENTS.md L56 警告的「silently diverge」。

**预期结果**：你将亲眼看到「不 configure 就改 toml，生成头不会跟着变」这一失效模式，从而理解为什么 AGENTS.md 把「重新 configure」写成不可商量的纪律。**实验结束后请务必把 toml 改回原值并重新 configure**，以免污染后续构建。本实践是否观察到预期现象，取决于本地 mtime 与文件系统行为，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：你 `git pull` 拉到新代码后，直接 `make -s` 报错「找不到某个目标」。最可能的原因是什么？第一步该做什么？
**参考答案**：最可能是新代码改了 Makefile / toml / 增删了目录，而你的 build 树还停留在旧 `configure` 的产物上（典型症状正是 AGENTS.md L55 说的「我改了 Makefile 却什么都没发生」）。第一步应从 `build/` 重新跑 `../configure`，再 `make -s`。

**练习 2**：你发现 SimX 跑出来的缓存行为和 RTL 不一致，怀疑是 `VX_config.h` 过期。能不能图省事，在某个 Makefile 里加 `-DVX_CFG_DCACHE_ENABLE=0` 把它「改对」？
**参考答案**：不能。AGENTS.md L56 明确禁止这种做法——往 Makefile 里塞 `-DVX_CFG_*` 覆盖只会掩盖过期的 `VX_config.h`，和配置系统（以 toml 为唯一真相来源）对着干，迟早再出问题。正确做法是找出 `VX_config.h` 为何过期（通常是没重新 configure），然后重新 `configure`，让 toml 重新驱动生成头。

---

## 5. 综合实践

**任务**：完整走一遍 Vortex 的「配置 → 工具链 → 构建」首次流程，并用一句话解释每一步在「模板填空机」模型里做了什么，最后回答「为什么我这台机器上能同时维护 32 位和 64 位两棵树」。

**操作步骤**：

1. 准备构建目录：

   ```bash
   cd <源码树根>
   mkdir -p build && cd build
   ```

2. 运行 configure（任选一位宽，本任务以 32 位为例）：

   ```bash
   ../configure --xlen=32 --tooldir=$HOME/tools
   ```

   *一句话*：这台「模板填空机」读取 `--xlen=32 --tooldir=...`，把它们烘焙进 `config.mk`、各 `Makefile`、`ci/*.sh`，并从 toml 生成 `hw/*.vh`、`sw/*.h`，造出一棵可运行的树。

3. 安装预编译工具链（首次；需联网）：

   ```bash
   ./ci/toolchain_install.sh
   ```

   *一句话*：按 `VERSION` 里的 `TOOLCHAIN_REV=v3.0`，从 GitHub 把 Verilator / RISC-V / LLVM / PoCL…… 全部解压到 `$HOME/tools`，供上一步写进 `config.mk` 的路径去引用。

4. 构建：

   ```bash
   make -s
   ```

   *一句话*：顶层 `Makefile`（`include config.mk`）依次驱动 `third_party → hardware → software → tests`，各子目录靠自己的 `common.mk` `include config.mk` 拿到工具路径，原地引用 `VORTEX_HOME` 下的源码完成编译。

5. **验证多树共存**：再建一棵 64 位树，互不干扰：

   ```bash
   cd <源码树根>
   mkdir -p build64 && cd build64
   ../configure --xlen=64 --tooldir=$HOME/tools
   grep '^XLEN\|RISCV_TOOLCHAIN_PATH' config.mk   # 应看到 xlen=64 与 riscv64 工具链
   ```

   然后回到 `build/`：

   ```bash
   cd ../build
   grep '^XLEN\|RISCV_TOOLCHAIN_PATH' config.mk   # 仍是 xlen=32 与 riscv32 工具链
   ```

**需要观察的现象 / 预期结果**：两棵树各自的 `config.mk` 自带不同的 `XLEN` 与 RISC-V 工具链前缀，互不影响。你能据此回答：「多棵树能共存，是因为 `configure` 把绝对路径与 `XLEN` 焊死进每棵树自己的 `config.mk`，彼此不依赖任何全局环境变量。」这就把本讲的三个最小模块（`configure` 脚本、工具链与 `config.mk`、重新 configure 的纪律）串成了一条完整的首次构建链路。

> 本综合实践需要联网下载工具链并实际编译，能否在当前环境跑通取决于网络与本机依赖；若工具链已就绪，预计可顺利完成。完整 `make -s` 的通过情况标注「待本地验证」。

## 6. 本讲小结

- Vortex 采用 **out-of-tree 构建**：源码树只读、共享，构建产物进独立的 `build/` 目录；`../configure` 在 `build/` 里生成一棵可运行的树。
- `configure` 是一台**模板填空机**：它解析 `--xlen/--tooldir/--osversion/--prefix`，用 `sed` 把 9 个 `@占位符@` 烘焙进 `config.mk`、各 `Makefile`、`ci/*.sh`，并用 `gen_config.py` 从 `*.toml` 生成 `hw/*.vh` 与 `sw/*.h`。
- 工具链分两段：`ci/toolchain_install.sh` 按 `VERSION` 的 `TOOLCHAIN_REV` 从 GitHub 下载预编译工具到 `$TOOLDIR`；`config.mk` 在 `$TOOLDIR` 下定义每个工具的子路径，再被全树 `common.mk` 通过 `include $(ROOT_DIR)/config.mk` 层层共享。
- `XLEN` 自动选择工具链：`config.mk` 里 `RISCV_TOOLCHAIN_PATH=$(TOOLDIR)/riscv$(XLEN)-gnu-toolchain`，故 `--xlen=32/64` 分别用 `riscv32-`/`riscv64-` 工具链。
- **多棵 Vortex 树能共存**：每棵树的 `config.mk` 自带互不相同的绝对路径与 `XLEN`，不依赖全局环境变量，故 `build32/` 与 `build64/` 可同机并存。
- **不可商量的纪律**（AGENTS.md §2）：`git pull`、改 Makefile / toml、增删构建目录后，必须从 `build/` 重新 `../configure`；尤其要警惕「stale `VX_config.h`」大坑——绝不能用 `-DVX_CFG_*` 覆盖来绕过，只能重新 configure 让 toml 这唯一真相来源重新生效。

## 7. 下一步学习建议

- 下一讲 **u1-l4《首次运行：用 blackbox.sh 跑通 demo》** 会用本讲生成的 `build/ci/blackbox.sh` 在 SimX 上跑通第一个程序，并介绍 `--cores/--warps/--threads/--driver` 等运行时旋钮。
- 想深入「配置值如何流到 RTL / 运行时 / 内核」的完整机理，进入 **U2 硬件配置系统**（u2-l1 起），那里会逐字段拆解 `VX_config.toml`、`VX_types.toml` 与 `gen_config.py` 的三种输出格式。
- 若你想了解工具链本身如何从源码构建（而非用预编译包），可读 [docs/building_toolchain.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/building_toolchain.md) 与 [docs/environment_setup.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/environment_setup.md)。
- 后续所有讲义里的命令都默认在 `build/` 目录下执行（AGENTS.md §4 规则），请把「先进 build 目录」养成肌肉记忆。
