# 基于 Make 的 HDL 仿真测试方法

## 1. 本讲目标

本讲是「核心方法学」单元的第一讲。学完后你应当能够：

- 看懂 Bedrock 用 GNU Make 驱动 Icarus Verilog 仿真的整套机制；
- 解释 `%_tb`、`%_check`、`%.vcd`、`%_view`、`%_lint` 这一组**模式规则（pattern rule）**各自编译/运行了什么命令；
- 用 `VFLAGS_<target>` 这种「按目标定制」的机制，为**某一个** testbench 单独追加参数，而不影响其它 testbench；
- 理解 iverilog 的 `-y`（模块搜索目录）与 `-M`（依赖输出）如何实现自动依赖推导；
- 读懂任意一个子目录的 `rules.mk`，并说出它是如何通过 `include` 复用 `top_rules.mk` 这套公共骨架的。

本讲不教你写 Verilog，也不讲任何一个 DSP 算法本身——那些是后续单元的事。本讲只讲一件事：**Bedrock 用什么「手势」把一段 Verilog 测试台变成一次可重复的仿真与校验**。掌握这个手势后，后面所有单元的 `make xxx_check` 你都能看懂、能改、能排错。

## 2. 前置知识

在进入正题前，先用最通俗的语言把几个术语对齐。本讲默认你已经读过 `u1-l2`（知道 Bedrock 的必需工具链是 Make + iverilog + python3，且 `selftest.sh` 本质就是逐目录 `make -C <dir>`）。

- **Make / Makefile**：一个 1976 年的老牌构建工具。它的核心想法极简——「**目标: 依赖**」加一行以 Tab 开头的 shell 命令。当依赖比目标新时，就执行那条命令来重建目标。本讲大量用到它的「自动变量」：`$@`（目标名）、`$<`（第一个依赖）、`$^`（全部依赖）。
- **iverilog（Icarus Verilog）**：一个开源的 Verilog 编译器。它把 `.v` 源码编译成一个可执行文件（默认不产生真实电路，而是产生可在仿真器上跑的代码）。
- **vvp**：iverilog 自带的仿真运行器。iverilog 编译出的文件，由 vvp 来「执行」，从而模拟出波形与打印。
- **testbench（测试台）**：一段专门用来「驱动 + 观测」某个模块的 Verilog 代码，文件名通常以 `_tb.v` 结尾。它本身不会被综合成电路，只用于仿真。
- **VCD（Value Change Dump）**：一种通用的波形文件格式。仿真时加 `+vcd` 选项就能 dump 出 `.vcd` 波形，再用 GTKWave 打开看。
- **模式规则（pattern rule）**：Make 的一种「通配规则」，用 `%` 当通配符。比如 `%_tb: %_tb.v` 一条规则就能匹配 `mixer_tb`、`complex_mul_tb` 等所有名字形如 `<X>_tb` 的目标，省去为每个模块各写一条。

> 关键直觉：软件项目里 Make 通常用来「编译链接出可执行程序」；而 Bedrock 用 Make 来「编译出仿真可执行文件 + 跑仿真 + 比对结果」。命令不一样，但 Make 的底层概念完全相同。这正是 `makefile.md` 想传达的第一句话。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [build-tools/makefile.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/makefile.md) | 方法学的**教程文档**，用一段从简到繁的演化讲清楚 Bedrock 为何这样用 Make。本讲的「为什么」几乎都来自这里。 |
| [build-tools/top_rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk) | 公共构建骨架。定义了 iverilog/vvp/gtkwave 的调用变量、所有模式规则、CDC/综合的规则。每个子目录都 `include` 它。 |
| [build-tools/bottom_rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/bottom_rules.mk) | 公共「收尾」骨架，主要提供统一的 `clean` 规则。 |
| [dsp/rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk) | `dsp` 子目录的**本地规则**：列出本目录有哪些 testbench（`TEST_BENCH`）、有哪些 check/lint 目标、以及模块搜索路径。 |
| [dsp/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/Makefile) | `dsp` 子目录的 Makefile 入口，只有 7 行，展示「三段 include」标准骨架。 |

一句话总结这张地图：**`makefile.md` 讲原理，`top_rules.mk` + `bottom_rules.mk` 是公共骨架，`dsp/Makefile` + `dsp/rules.mk` 是某个子目录如何「插上」这套骨架。**

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，由浅入深：先看 Make 模式规则本身（4.1），再看 Bedrock 把它落地成哪几条仿真规则（4.2），接着看「按目标定制」与「依赖推导」两个进阶机制（4.3），最后看一个子目录如何整体接入（4.4）。

### 4.1 从手写到通配：Make 模式规则入门

#### 4.1.1 概念说明

`makefile.md` 的作者刻意没有一上来就甩出 Bedrock 真实的 `top_rules.mk`，而是先给了一段「最笨的、能跑的」Makefile，再一步步把它「抽象 + 复用」。这种写法非常值得学习，因为它揭示了复杂 Makefile 的来龙去脉。

理解这一节的核心是「**同一个目标，可以有很多种等价的 Makefile 写法**」。Bedrock 最终选择的那一种，是为了让「新增一个模块的测试」这件事几乎零成本——只改一个变量，不改任何规则。

#### 4.1.2 核心流程

`makefile.md` 的演化分四步：

1. **手写阶段**：为每个 testbench 各写一条完整规则，命令里写死文件名。能跑，但每加一个模块就要复制粘贴一整段。
2. **模式规则阶段**：用 `%` 通配，把「所有 `_tb` 目标」抽象成一条规则 `%_tb: %_tb.v`，命令用 `$@`/`$<` 引用。
3. **可配置阶段**：把命令里写死的 `iverilog` 抽成变量 `VERILOG`，于是命令行可以 `make VERILOG=iverilog-0.10` 临时换版本。
4. **共享阶段**：把所有通用定义集中到 `top_rules.mk`，各目录 `include` 它，本地只留「本目录特有的东西」。

把这四步串起来，就理解了 Bedrock 整套构建系统的设计动因。

#### 4.1.3 源码精读

先看「最笨」的写法。`makefile.md` 给出的起点 Makefile 里，每个 testbench 都是显式的两条规则（编译 + 运行）：

[build-tools/makefile.md:29-51](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/makefile.md#L29-L51) —— 文档给出的「手写版」示例，`b2d_tb` 与 `fib_tb` 各自各写一条 `iverilog -o ...` 与 `vvp -N ...`。注意它演示了两种校验风格：`b2d_tb` 是**自校验**（失败 `$stop`、成功 `$finish`），而 `fib_tb` 把输出重定向到文件再与 `fib.gold` 比对。

接着文档用「模式规则 + 自动变量」把上面这一大段压缩成几行。`%` 是通配符，`$@` 是目标名、`$<` 是首依赖、`$^` 是全部依赖：

[build-tools/makefile.md:67-98](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/makefile.md#L67-L98) —— 「泛化版」Makefile。`%_check: %_tb` 一条规则覆盖所有 check；`%_tb: %_tb.v` 一条规则覆盖所有编译。这是理解 `top_rules.mk` 的关键前置。

> 想验证这两份 Makefile「等价」？文档末尾专门给了一段 `iverilog ... vvp ... PASS` 的真实输出作为交叉核对：[build-tools/makefile.md:217-243](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/makefile.md#L217-L243)。

#### 4.1.4 代码实践

这是一个「源码阅读 + 心算」型实践，目标是让你亲手体会「手写版」与「模式版」的等价性。

1. **实践目标**：理解模式规则如何代替逐条手写。
2. **操作步骤**：阅读 [build-tools/makefile.md:78-98](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/makefile.md#L78-L98)。对 `fib_tb` 这个目标，分别写出「手写版」和「模式版」会执行的命令。
3. **需要观察的现象**：两版命令的字符串应当完全一致（忽略空格）。
4. **预期结果**：两版都执行 `iverilog -o fib_tb fib_tb.v fib.v`。模式版里 `%` 匹配 `fib`，故 `$@=fib_tb`、`$^=fib_tb.v fib.v`。
5. **运行结果**：待本地验证（无需真正运行，推理即可；若要实测，可把文档里的示例 Makefile 抄到临时目录，前提是有 `fib.v`/`b2d.v` 等示例源码）。

#### 4.1.5 小练习与答案

**练习 1**：在模式规则 `%_tb: %_tb.v` 中，要构建目标 `complex_mul_tb`，`%` 匹配到的字符串是什么？`$<` 又是什么？

**答案**：`%` 匹配 `complex_mul`；`$<` 是第一个依赖，即 `complex_mul_tb.v`。

**练习 2**：为什么文档说 `echo PASS` 前面要加 `@`？

**答案**：Make 默认会先把每条命令「回显」再执行。`@` 前缀抑制回显。`cmp` 的回显有助于排错（要看到），而 `echo PASS` 只关心它的结果、不想看到命令本身，故加 `@`。

---

### 4.2 top_rules.mk：把模式规则变成可复用的仿真骨架

#### 4.2.1 概念说明

`top_rules.mk` 是整个 Bedrock 构建系统的「公共底盘」。它做了两件事：

1. **定义一堆「配方变量」**：把 `iverilog`、`vvp`、`gtkwave` 的调用方式写成带占位符的变量（如 `VERILOG_TB`），这样所有模式规则都复用同一份「配方」，改一处即全局生效。
2. **定义一组模式规则**：`%_tb`（编译测试台）、`%_check`（跑仿真校验）、`%.vcd`（生成波形）、`%_view`（看波形）、`%_lint`（静态 lint）。

理解它的关键是：**这些规则的「依赖」串成了一条流水线**。`%_check` 依赖 `%_tb`，`%_tb` 依赖 `%_tb.v`。所以你只要敲 `make complex_mul_check`，Make 会自动先把 `complex_mul_tb` 编译出来，再跑它。

#### 4.2.2 核心流程

一个典型的「编译→仿真→校验」链条如下（以 `complex_mul` 为例）：

```
complex_mul_tb.v   ──(%_tb: %_tb.v, 配方 VERILOG_TB)──►   complex_mul_tb   (iverilog 编译产物)
complex_mul_tb     ──(%_check: %_tb, 配方 VERILOG_CHECK)──►  仿真运行 + 自校验
complex_mul_tb     ──(%.vcd: %_tb, 配方 VERILOG_SIM)─────►  complex_mul.vcd  (波形)
complex_mul.vcd + .gtkw ──(%_view)──────────────────────►  gtkwave 打开波形
```

注意：`%_check`、`%.vcd`、`%_view` 都把 `%_tb` 当作中间依赖，所以它们都会**先触发编译**。这意味着「敲一个 check，编译也会自动完成」——你几乎永远不需要手动先 `make xxx_tb`。

#### 4.2.3 源码精读

先看「配方变量」是怎么定义的。这几个变量是后面所有模式规则的弹药库：

[build-tools/top_rules.mk:48-56](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L48-L56) —— 关键配方。逐条解读：

- `VERILOG_COMP`：编译普通（非测试台）Verilog。
- `VERILOG_TB`：**编译测试台**。注意 `$(filter %v, $^)`——它从「全部依赖」里挑出所有以 `v` 结尾的文件（`.v`/`.sv`）一起喂给 iverilog。这就是为什么在 `rules.mk` 里写 `rot_dds_tb: cordicg_b22.v ph_acc.v` 这种「额外依赖」能自动把新文件加进编译列表。
- `VERILOG_SIM`：跑仿真并 dump 波形。`cd $(dirname $@)` 是为了让 `.vcd` 落在正确目录。
- `VERILOG_VIEW`：用 gtkwave 打开 `.vcd` + `.gtkw`。
- `VERILOG_CHECK`：跑仿真做自校验（`$(VVP) $<`）。

再看核心的几条模式规则，它们把上面的配方接上依赖：

[build-tools/top_rules.mk:102-105](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L102-L105) —— `%_tb: %_tb.v`，用 `VERILOG_TB` 编译。注意它被 `ifndef NO_DEFAULT_TB_RULE` 包起来：个别子目录若想自定义编译流程，可以 `NO_DEFAULT_TB_RULE=1` 关掉这条默认规则。

[build-tools/top_rules.mk:119-126](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L119-L126) —— 三条最常用的规则：
- `%.vcd: %_tb` → `VERILOG_SIM`，加 `+vcd` 与可选的 `$(VCD_ARGS)`；
- `%_view: %.vcd %.gtkw` → `VERILOG_VIEW`，注意它**额外依赖 `.gtkw` 波形配置**，没有 `.gtkw` 就没法 view；
- `%_check: %_tb` → `VERILOG_CHECK`，跑仿真做校验。

[build-tools/top_rules.mk:128-129](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L128-L129) —— `%_lint: %.v %_auto`，用 Verilator 做**静态 lint**（不仿真，只检查语法/可疑写法）。它依赖 `.v` 源码和一个 `%_auto` 目标（用于触发自动生成代码）。

> 还有一条「活化石」规则值得一看：`%_tb_vpi`（[top_rules.mk:110-111](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L110-L111)）用于带 VPI（C 语言扩展）的测试台，编译命令略不同。本讲不展开，知道有这回事即可。

#### 4.2.4 代码实践

这是本讲的主打动手实践，使用 `dsp` 下确实存在测试台的 `complex_mul` 模块（它有 `complex_mul_tb.v`，且在 `TEST_BENCH` 名单内）。

1. **实践目标**：亲眼看 `%_tb` / `%_check` / `%.vcd` 三条规则分别调用了什么命令。
2. **操作步骤**（在仓库根目录执行；等价于先 `cd dsp` 再 `make`）：
   ```bash
   make -C dsp complex_mul_tb      # 编译测试台
   make -C dsp complex_mul_check   # 跑仿真自校验
   make -C dsp complex_mul.vcd     # 生成波形
   ```
3. **需要观察的现象**：Make 在执行每条命令前会先「回显」这条命令。重点看回显：
   - `complex_mul_tb` 应回显一条 `iverilog ... -o complex_mul_tb ...`；
   - `complex_mul_check` 应回显一条 `vvp -N complex_mul_tb`；
   - `complex_mul.vcd` 应回显一条 `cd . && vvp -N complex_mul_tb +vcd`。
4. **预期结果**：三条命令的形状与 4.2.3 中 `VERILOG_TB` / `VERILOG_CHECK` / `VERILOG_SIM` 的定义一一对应。`complex_mul_check` 仿真结尾应打印类似 `N tests passed` / `PASS`（具体措辞待本地验证）。
5. **运行结果**：待本地验证（仿真是否 PASS 取决于本地工具链版本）。

> **故意踩个坑（可选）**：同样地试一下 `make -C dsp mixer_tb`。你会得到 `No rule to make target 'mixer_tb'`。原因：模式规则 `%_tb: %_tb.v` 要求存在 `mixer_tb.v`，而 `dsp/` 下只有 `mixer.v`、没有 `mixer_tb.v`（且 `mixer_tb` 不在 `TEST_BENCH` 名单）。这正是 `u1-l3` 讲过的「mixer 只被上层模块间接测试」。这个报错是**预期行为**，不是 bug——它很好地印证了「模式规则只指明去哪找，不保证一定存在」。

#### 4.2.5 小练习与答案

**练习 1**：为什么敲 `make complex_mul_check` 时不需要先手动 `make complex_mul_tb`？

**答案**：因为 `%_check: %_tb` 把 `complex_mul_tb` 列为依赖。Make 发现依赖不存在或过期，会先递归构建它，再执行 check 的命令。

**练习 2**：`%_view` 规则的依赖里除了 `.vcd` 还有 `.gtkw`。如果某个模块只有 `.vcd` 没有 `.gtkw`，`make xxx_view` 会怎样？

**答案**：Make 找不到 `xxx.gtkw`，又没有规则能生成它，于是报 `No rule to make target 'xxx.gtkw'`，view 失败。`.gtkw` 是 gtkwave 的波形布局配置，需要手写或从 gtkwave 里保存。

**练习 3**：`%_lint` 和 `%_check` 都能「检查」一个模块，本质区别是什么？

**答案**：`%_lint` 用 Verilator 做**静态**检查，不跑仿真、不产生波形，速度快，主要抓语法与可疑写法；`%_check` 用 vvp **动态**跑一遍仿真，验证功能行为是否正确。

---

### 4.3 按目标定制参数与自动依赖推导

#### 4.3.1 概念说明

上两节讲的规则是「一刀切」的：所有 testbench 用同一套 iverilog 参数。但现实里常常有这种需求——**只给某一个 testbench 加一个宏定义、或挂一个 VPI 模块，其它 testbench 不受影响**。`makefile.md` 把这个需求称为「One more trick」，Bedrock 用 `VFLAGS_<target>` 机制优雅地解决了它。

本模块还要解决第二个问题：**iverilog 怎么知道一个 testbench 用到了哪些子模块文件？** 手写依赖太痛苦，Bedrock 借助 iverilog 的 `-y`（模块搜索目录）与 `-M`（输出依赖列表）做自动依赖推导。

#### 4.3.2 核心流程

**按目标定制的原理**：在 `VFLAGS` 的定义里塞进一个「以目标名为后缀的变量」：

```
VFLAGS = ${VFLAGS_$@} -I$(AUTOGEN_DIR)
              └─┬───┘
                └─ 当目标是 xxx_tb 时，这里展开成变量 VFLAGS_xxx_tb 的值
```

于是，只要在某处写一句 `VFLAGS_b2d_tb = -m ./udp-vpi`，构建 `b2d_tb` 时就会多出 `-m ./udp-vpi`，而构建其它 `_tb` 时这个变量是空的、完全不受影响。命令行也能临时覆盖：`make VFLAGS_complex_mul_tb=-DXXX complex_mul_tb`。

**依赖推导的原理**：

- `-y <dir>`：告诉 iverilog「遇到未解析的模块名，就到 `<dir>` 里找文件名与模块名匹配的 `.v`」。
- `-M`：让 iverilog 在编译时顺带输出「本次实际用到了哪些文件」的列表。把这个列表写成 Make 的依赖格式，就有了自动依赖。

两者配合，就能让 `complex_mul_tb.v` 自动「拉进」它实例化的所有子模块文件，无需手写。

#### 4.3.3 源码精读

先看「按目标定制」的钩子是怎么埋进 `VFLAGS` 的：

[build-tools/top_rules.mk:11-16](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L11-L16) —— 第 14 行 `VFLAGS = ${VFLAGS_$@} -I$(AUTOGEN_DIR)` 就是那个钩子。`VFLAGS_$@` 在求值时会变成「`VFLAGS_` 拼上当前目标名」。同时这一行还把自动生成目录 `_autogen`（即 `AUTOGEN_DIR`）默认加入头文件搜索路径。

这个「 trick 」的出处和动机，`makefile.md` 有专门一节解释：

[build-tools/makefile.md:186-200](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/makefile.md#L186-L200) —— 「One more trick」。`VFLAGS_b2d_tb = -m ./udp-vpi` 这个例子展示了：给 `b2d_tb` 单独挂一个 VPI 模块，而不打扰其它 `_tb` 的构建命令。

再看一个**真实**的 per-target 定制案例，就在 `dsp/rules.mk` 里：

[dsp/rules.mk:41](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L41) —— `VFLAGS_rx_buffer_tb = -DTARGET_s3`。构建 `rx_buffer_tb` 时会自动加上 `-DTARGET_s3` 这个宏定义。这就是 4.3.2 所述机制的活用。

接着看依赖推导。`makefile.md` 用一段话点明了思路：

[build-tools/makefile.md:176-184](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/makefile.md#L176-L184) —— 「Dependencies」一节：`-y` 让 iverilog 按模块名搜目录，`-M` 输出实际用到的文件列表，后者可转成 Make 依赖。

`top_rules.mk` 里把它落地的配方叫 `MAKEDEP`：

[build-tools/top_rules.mk:59](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L59) —— `MAKEDEP = $(VERILOG) ... -o /dev/null -M$@.$$$$ $<`。它跑一次 iverilog（输出丢弃到 `/dev/null`，因为目的不是编译而是拿依赖），用 `-M` 把依赖列表写到一个临时文件 `$@.$$$$`。

然后把临时文件整理成标准 `.d` 依赖文件的模式规则：

[build-tools/top_rules.mk:213-214](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L213-L214) —— `$(DEPDIR)/%_tb.d: %_tb.v ...`：先 `mkdir -p $(DEPDIR)`，再跑 `MAKEDEP`，最后把 `$@.$$$$` 里的文件列表排序去重、格式化成 `xxx_tb <depfile>: a.v b.v c.v` 写进 `.d` 文件。这个 `.d` 文件随后会被 `include` 进 Make（见 4.4.3），于是「改了某个子模块源码 → 对应 testbench 自动重编」就成立了。

> 注意 `dsp/rules.mk` 里 `VFLAGS` 与 `VFLAGS_DEP` 都拼上了一串 `-y` 目录（[dsp/rules.mk:5-6](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L5-L6)）：`-y . -y$(DSP_DIR) -y$(CORDIC_DIR)`。这正是给 `-y` 模块搜索提供搜索路径——`dsp` 的模块既可能引用本目录、也可能引用 `dsp/` 与 `cordic/` 下的模块。

#### 4.3.4 代码实践

验证「按目标定制」是否真的生效。

1. **实践目标**：亲眼看到命令行传入的 `VFLAGS_<target>` 被插进 iverilog 命令。
2. **操作步骤**：
   ```bash
   # 先正常编译，记下回显的命令
   make -C dsp complex_mul_tb
   # 再带一个临时宏编译，对比回显
   make -C dsp VFLAGS_complex_mul_tb=-DXXX complex_mul_tb
   ```
   （注意：第二次要先 `make -C dsp clean` 或删除 `complex_mul_tb`，否则 Make 认为目标已是最新的、不会重新执行命令，你就看不到差异。）
3. **需要观察的现象**：第二次的回显命令里，`iverilog` 后面应当多出 `-DXXX`。
4. **预期结果**：根据 [top_rules.mk:14](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L14) 与 [top_rules.mk:49](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L49)，`VFLAGS` 会展开为 `-DXXX -I_autogen`，故 iverilog 命令形如 `iverilog -DSIMULATE -Wno-timescale -DXXX -I_autogen -o complex_mul_tb complex_mul_tb.v`。`-DXXX` 定义了一个无人使用的宏，编译仍应成功。
5. **运行结果**：待本地验证（命令形状可由 Makefile 确定性推出；是否真的多出 `-DXXX` 以本地回显为准）。

#### 4.3.5 小练习与答案

**练习 1**：为什么命令行写 `make VFLAGS_complex_mul_tb=-DXXX`，而不是 `make VFLAGS=-DXXX`？

**答案**：`VFLAGS` 在 `top_rules.mk` 里是被**赋值**的（`VFLAGS = ${VFLAGS_$@} ...`）。如果在命令行覆盖 `VFLAGS` 整体，会冲掉 `${VFLAGS_$@}` 与 `-I$(AUTOGEN_DIR)`，影响所有目标。而 `VFLAGS_complex_mul_tb` 是一个独立变量，只在 `VFLAGS` 内部被引用，覆盖它只影响 `complex_mul_tb` 一个目标。

**练习 2**：依赖推导为什么要跑一次「输出丢弃」的 iverilog（`-o /dev/null`）？

**答案**：目的是借 iverilog 的解析能力拿到「实际用到了哪些文件」（`-M` 输出），而不是真的要编译产物。把产物丢到 `/dev/null` 避免产生垃圾文件，同时仍能得到依赖列表。

**练习 3**：`-y` 和 `-I` 都能让 iverilog 找到文件，区别是什么？

**答案**：`-I` 指定 `` `include `` 头文件（`.vh`）的搜索目录；`-y` 指定**模块名**的搜索目录——遇到未解析的模块 `foo`，iverilog 会到 `-y` 目录下找 `foo.v`。两者解决的是不同的问题。

---

### 4.4 dsp/rules.mk：子目录如何接入构建系统

#### 4.4.1 概念说明

前三个模块都在讲「公共骨架」。本模块回答：**一个具体子目录（以 `dsp` 为例）怎么「插上」这套骨架？** 答案是「三段式 Makefile」加一份本地 `rules.mk`。

`u1-l3` 已经提过这个骨架的形状，本讲把它和仿真规则彻底打通：子目录要做的，本质上只有三件事——**告诉 Make「我有哪些 testbench」「我的模块在哪些目录里搜」「我有哪些特别的 target」**，其余全部继承自 `top_rules.mk`。

#### 4.4.2 核心流程

一个子目录的构建接入分三步：

1. 写一个极简 `Makefile`，按固定顺序 `include` 三段：`dir_list.mk`（拿到目录常量）→ `top_rules.mk`（公共规则）→ 本地 `rules.mk`（夹在中间）→ `bottom_rules.mk`（clean 收尾）。
2. 在本地 `rules.mk` 里填 `TEST_BENCH` 变量，列出本目录所有 testbench（**不带** `_tb` 后缀……其实是带 `_tb` 后缀的「目标基名」列表）。
3. 用 Make 的替换函数把 `TEST_BENCH` 自动派生出 `checks`、`lint` 等聚合目标，于是 `make checks` 能一次跑完本目录全部校验。

#### 4.4.3 源码精读

先看「三段式」骨架本体——`dsp/Makefile` 全文只有 7 行：

[dsp/Makefile:1-7](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/Makefile#L1-L7) —— 解读：
- `include ../dir_list.mk`：拿到 `$(DSP_DIR)`、`$(BUILD_DIR)` 等绝对路径变量（`u1-l3` 讲过 `dir_list.mk` 是仓库「地址簿」）。
- `VERILOG_AUTOGEN += "cordicg_b22.v"`：声明 `cordicg_b22.v` 是自动生成文件（由 `cordicgx.py` 生成，见后续 `u3-l1`），让依赖推导知道它。
- `include $(BUILD_DIR)/top_rules.mk`：把公共规则拉进来。
- `include rules.mk`：把本地规则夹在 `top_rules.mk` **之后**（这样本地规则能用上公共变量）、`bottom_rules.mk` **之前**。
- `include $(BUILD_DIR)/bottom_rules.mk`：拉入统一的 `clean`。

`bottom_rules.mk` 提供的统一 `clean`：

[build-tools/bottom_rules.mk:1-12](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/bottom_rules.mk#L1-L12) —— `clean::` 用双冒号（允许追加），删除 `$(CLEAN)` 与 `$(CLEAN_DIRS)`，最后还跑 `$(CHECK_CLEAN)` 做源码卫生检查（无隐藏文件、无尾随空格等）。各子目录只需往 `CLEAN` 里追加自己的生成物即可。

再看本地 `rules.mk` 的核心——`TEST_BENCH` 与聚合目标：

[dsp/rules.mk:8-16](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L8-L16) —— 解读：
- 第 8 行 `TEST_BENCH = data_xdomain_tb upconv_tb ... freq_count_tb`：本目录全部 testbench 列表（注意每个都带 `_tb` 后缀）。**`mixer_tb` 不在里面**，再次印证 mixer 无独立测试台。
- 第 10 行 `TGT_ := $(TEST_BENCH)`：「编译全部」的目标集合。
- 第 12 行 `NO_CHECK = piloop2_check cavity_check banyan_mem_check`：这几个虽然能编译，但**不能**自动化 check（可能需要人工看波形），故排除。
- 第 13 行 `CHK_ = $(filter-out $(NO_CHECK), $(TEST_BENCH:%_tb=%_check))`：经典用法——`$(TEST_BENCH:%_tb=%_check)` 把每个 `xxx_tb` 替换成 `xxx_check`，再用 `filter-out` 剔除 `NO_CHECK` 里列的。于是 `CHK_` 就是「所有可自动 check 的目标」。
- 第 15–16 行同理造出 `LNT_`（lint 目标集合）。

把这些集合挂到 phony 聚合目标上：

[dsp/rules.mk:22-27](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L22-L27) —— `targets: $(TGT_)`、`checks: $(CHK_)`、`lint: $(LNT_)`。这就是为什么 `make -C dsp checks` 能一次跑完几十个 check——它依赖一整个集合，Make 会逐个构建。而每个 `xxx_check` 又通过 `top_rules.mk` 的 `%_check: %_tb` 自动拉起编译与仿真。

最后看「按需 include 依赖文件」的精妙之处：

[dsp/rules.mk:104-122](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L104-L122) —— 根据 `MAKECMDGOALS`（你在命令行敲的目标）**有选择地** include 对应的 `.d` 依赖文件。比如你敲 `make complex_mul_check`，就只 include `_dep/complex_mul_tb.d`，而不是把几十个 `.d` 全 include 进来。这避免了「改一个文件触发全库重新推导」的开销，也避免未生成的 `.d` 报错。第 120–122 行：当没指定目标（裸 `make`）时，才 include 全部 `TEST_BENCH` 的 `.d`。

#### 4.4.4 代码实践

跑一遍聚合目标，感受「一个命令跑完一组校验」。

1. **实践目标**：理解 `make checks` 如何由 `TEST_BENCH` 自动派生。
2. **操作步骤**：
   ```bash
   make -C dsp checks 2>&1 | tail -40    # 跑本目录全部可自动 check 的目标
   ```
3. **需要观察的现象**：Make 会依次回显几十条 `vvp -N xxx_tb`，每个对应 `TEST_BENCH` 里的一个模块（减去 `NO_CHECK`）。
4. **预期结果**：能看到 `complex_mul_tb`、`biquad_tb`、`cic_multichannel_tb` 等被依次仿真；**不会**看到 `mixer`。若本地缺某些工具，对应项会被跳过而非整体失败（与 `u1-l2` 的 selftest 行为一致）。
5. **运行结果**：待本地验证（具体多少项 PASS 取决于本地工具链是否齐全）。

#### 4.4.5 小练习与答案

**练习 1**：`dsp/rules.mk` 里 `CHK_ = $(filter-out $(NO_CHECK), $(TEST_BENCH:%_tb=%_check))`，请把 `TEST_BENCH = a_tb b_tb`、`NO_CHECK = b_check` 代入，算出 `CHK_`。

**答案**：`$(TEST_BENCH:%_tb=%_check)` 先把 `a_tb b_tb` 变成 `a_check b_check`；`filter-out b_check` 剔除 `b_check`，剩下 `a_check`。故 `CHK_ = a_check`。

**练习 2**：为什么本地 `rules.mk` 要 `include` 在 `top_rules.mk` **之后**？

**答案**：本地规则要用 `top_rules.mk` 里定义的变量（如 `VFLAGS`、`$(VVP)`、`$(PYTHON)`、`VERILOG_CHECK`）。先 include 公共骨架，本地规则才能引用这些变量；否则变量未定义，展开为空。

**练习 3**：`dsp/Makefile` 第 2 行 `VERILOG_AUTOGEN += "cordicg_b22.v"` 的作用是什么？

**答案**：声明 `cordicg_b22.v` 是自动生成文件。依赖推导规则（如 [top_rules.mk:213](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L213)）把它列为依赖，确保用到 CORDIC 的 testbench 在编译前先触发 `cordicg_b22.v` 的生成。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「追踪一条命令的全链路」任务。

**任务**：以 `make -C dsp complex_mul_check` 为研究对象，回答下列问题，并尽量**先预测、再实测验证**。

1. **依赖链还原**：画出从 `complex_mul_check` 出发的完整依赖树（它依赖谁？那个目标又依赖谁？最终依赖哪些 `.v` 文件？）。提示：结合 [top_rules.mk:125-126](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L125-L126) 的 `%_check: %_tb` 与 [top_rules.mk:213-214](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L213-L214) 的依赖推导规则。
2. **命令预测**：在动手运行前，**写出**你认为 Make 会执行的两条命令（编译 + 仿真）的完整字符串，依据是 [top_rules.mk:49](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L49) 与 [top_rules.mk:53](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L53)。
3. **实测对比**：运行 `make -C dsp complex_mul_check`，把回显的真实命令与你预测的逐字对比，解释每一处差异（例如多出的 `-y`、`-I` 来自哪里——应来自 [dsp/rules.mk:5-6](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L5-L6)）。
4. **依赖文件检查**：运行后查看 `_dsp/_dep/complex_mul_tb.d`（路径可能在 `_dep/` 下，待本地确认），列出 `complex_mul_tb` 实际依赖的全部 `.v` 文件，并解释为什么这些文件会自动出现（而不是手写在 `rules.mk` 里）。
5. **定制化实验**：用 `make -C dsp VFLAGS_complex_mul_tb=-DXXX complex_mul_check` 重新运行，确认 `-DXXX` 同时出现在**编译**命令里，但**不**出现在仿真（`vvp`）命令里，并解释原因（`VFLAGS` 只进 `VERILOG_TB`，不进 `VERILOG_CHECK`）。

**交付物**：一张依赖树图 + 一份「预测命令 vs 实测命令」对照表 + 第 4、5 步的现象说明。完成后，你就真正掌握了 Bedrock 的 Make 仿真方法学——后续任何单元的 `make xxx_check`，你都能拆解到命令级别。

> 说明：第 3、4、5 步的真实输出依赖本地工具链（iverilog/vvp 是否安装、版本号），凡涉及具体 PASS/FAIL 或文件路径细节处，请以本地实测为准（待本地验证）。命令的「形状」则完全由 Makefile 确定，可以 confidently 预测。

## 6. 本讲小结

- Bedrock 用 GNU Make 驱动 iverilog 仿真，核心是一组**模式规则**：`%_tb`（编译）、`%_check`（仿真自校验）、`%.vcd`（波形）、`%_view`（gtkwave 看波）、`%_lint`（Verilator 静态检查），它们共享同一份「配方变量」。
- 这组规则形成**依赖流水线**：敲一个 `make xxx_check`，Make 会自动先编译 `xxx_tb` 再仿真，几乎不需要手动分步。
- 「按目标定制」靠 `VFLAGS = ${VFLAGS_$@} ...` 这个钩子：写一句 `VFLAGS_xxx_tb = ...`（或命令行 `make VFLAGS_xxx_tb=...`）就能只给某一个 testbench 加参数，不影响其它目标。
- 自动依赖推导靠 iverilog 的 `-y`（按模块名搜目录）与 `-M`（输出用到的文件列表），生成的 `.d` 文件被按需 `include`，实现「改子模块源码 → 相关 testbench 自动重编」。
- 子目录通过「三段式 Makefile」（`dir_list.mk` → `top_rules.mk` → 本地 `rules.mk` → `bottom_rules.mk`）接入公共骨架，本地只需填 `TEST_BENCH` 列表与少量特例。
- **重要现实**：并非每个模块都有 `_tb` 目标——`mixer` 只有 `mixer.v` 而无 `mixer_tb.v`，故 `make mixer_tb` 会失败；模式规则只规定「去哪找」，不保证「一定存在」。

## 7. 下一步学习建议

本讲掌握了「怎么测」，下一讲 `u2-l2 片上 localbus 总线` 将进入「测什么」的第一个真实主题——Bedrock 贯穿全局的轻量总线 localbus，以及如何用 `localbus.vh` 里的 `lb_write_task`/`lb_read_task` 写周期精确的总线测试台。

在进入下一讲前，建议你：

- 用本讲学到的方法，亲手在 `dsp/` 跑通 2~3 个 `xxx_check`（如 `complex_mul_check`、`biquad_check`），建立肌肉记忆；
- 通读一遍 [build-tools/makefile.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/makefile.md) 全文，它是本讲最权威的配套读物；
- 后续阅读任何子目录时，先打开它的 `Makefile` + `rules.mk`，看清 `TEST_BENCH` 与 `VFLAGS` 定制，再看模块源码——这是最高效的 Bedrock 源码阅读姿势。
