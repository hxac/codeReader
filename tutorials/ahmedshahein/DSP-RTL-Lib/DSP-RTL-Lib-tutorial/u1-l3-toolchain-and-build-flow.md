# 工具链与构建运行流程

## 1. 本讲目标

本讲是「把 DRL 真正跑起来」的关键一讲。学完后你应当能够：

- 看懂 `dsp_rtl_lib.sh` 这个唯一的入口脚本：它有哪些子命令、各自做什么、失败时返回什么退出码。
- 理解 `.param` 文件的「键 = 值」格式，以及脚本如何用 `sed` 把参数值「文字替换」进 RTL 与 Octave 黄金参考模型（GRM）。
- 在脑海中（以及在本机）跑通一条完整流水线：`.param` → 复制模板 → sed 注入参数 → Octave 生成激励/响应 → Icarus Verilog 编译仿真 → 测试台逐样本比对 → 输出 PASSED/FAILED。

本讲承接 [u1-l2 仓库结构与目录组织](u1-l2-repository-structure.md)：你已经知道 `.drl_src_code` 是只读模板库、`.drl_param` 是参数库，而「构建产物」是脚本现场生成的。本讲就来讲清楚这套「现场生成」的机器到底怎么转。

## 2. 前置知识

在进入源码前，先用大白话澄清几个概念：

- **入口脚本（entry script）**：一个 Bash 脚本，所有操作（克隆、检查环境、生成设计、跑仿真）都从它发起。DRL 只有一个：`dsp_rtl_lib.sh`。
- **退出码（exit code）**：程序结束时返回给操作系统的一个整数。0 表示成功，非 0 表示某种失败。DRL 给每一种失败都约定了一个专属退出码（1~10），方便你在 CI 里精确判断「为什么挂了」。
- **参数注入（parameter injection）**：DRL 的 RTL 模板里写的是「默认参数」（例如 `gp_order = 3`）。脚本不去解析 Verilog 语法，而是用文本替换工具 `sed`，把默认值改成 `.param` 文件里指定的值。这是一种「够用但脆弱」的工程做法，理解它有助于你日后调试「为什么改了参数没生效」。
- **黄金参考模型（GRM, Golden Reference Model）**：用 Octave（MATLAB 的免费替代）写的一份「标准答案」程序。它根据参数生成输入激励（stimuli）和期望输出（response），RTL 仿真结果与之逐比特比对。
- **回归（regression）**：一次跑完一整套测试用例（DRL 里是 9 个），全部 PASSED 才算通过。这比你手动看一眼波形可靠得多。

如果你对 **shell 的 `case` 分支、`while` 循环、变量替换 `$1`** 还不熟，建议先花 10 分钟翻一眼 Bash 基础——本讲会用，但不会从零教 shell。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| `dsp_rtl_lib.sh` | 唯一入口脚本，约 754 行 | 子命令解析、退出码、sed 注入、仿真流水线 |
| `.drl_param/filt_cicd_1.param` | CIC 抽取滤波器的一个参数实例 | 讲清 `.param` 格式 |
| `.drl_src_code/filt_cicd/octave/stimuli.m` | CIC 的 Octave GRM 主程序 | sed 注入目标之一；生成激励/响应/defines |
| `.drl_src_code/filt_cicd/octave/gen_defines.m` | 生成 `defines_N.sv` 的辅助函数 | 把参数变成测试台宏 |
| `.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv` | SystemVerilog 测试台 | 比对逻辑、PASSED/FAILED 判定 |

记住 [u1-l2](u1-l2-repository-structure.md) 的核心结论：`filt_cicd/octave/stimuli.m`、`filt_cicd/sim/testbench/...` 这些都是「模板」，脚本会把它们 `cp` 到工作目录、改完参数后再用，原始模板始终只读。

## 4. 核心概念与源码讲解

### 4.1 命令行参数解析与子命令体系

#### 4.1.1 概念说明

`dsp_rtl_lib.sh` 是一个「多用途」脚本：克隆仓库、检查工具链、生成设计、跑单次仿真、一键演示、创建新模块脚手架，全靠命令行参数（子命令）来切换。它的设计哲学是「一个脚本管全流程」，而不是「每个功能一个脚本」。

脚本首先处理两个边界情况：

1. 无参数 → 报错退出（退出码 2）。
2. `-help` / `-h` → 打印一大段帮助并退出（退出码 3）。

其余情况进入一个 `case` 循环，逐个吞掉命令行 token，把对应的开关变量（`CONFG_*`）置为 `true`。

#### 4.1.2 核心流程

子命令到「被设置的开关变量」的映射如下：

| 子命令 | 缩写 | 设置的开关 | 作用 |
|--------|------|-----------|------|
| `-chk` | `-c` | `CONFG_CHK` | 自动探测已安装的 RTL 工具（iverilog→verilator→modelsim） |
| `-tool X` | `-t X` | `CONFG_TOOL` | 手动指定工具链（会清空 `CONFG_CHK`） |
| `-git` | `-g` | `CONFG_GIT` | 克隆仓库并做目录完整性自检 |
| `-path P` | `-p P` | `CONFG_PATH` | 指定生成设计的输出目录 |
| `-design F` | `-d F` | `CONFG_DSN` | 按参数文件 `F` 生成并仿真一个模块 |
| `-sim D T` | `-s D T` | `CONFG_SIM` | 对已生成的设计 `D` 跑第 `T` 号测试用例 |
| `-demo` | — | `CONFG_DEMO`+`CHK`+`DSN` | 一键演示（用内置的 `filt_cicd_1.param`） |
| `-dev` | `-v` | `CONFG_DEV` | 创建新模块脚手架（详见 [u7-l3](u7-l3-dev-mode-scaffolding.md)） |

流程伪代码：

```
if 参数为空:            exit 2
if 第一个参数是 -help:   打印帮助; exit 3
until 参数耗尽:
    case 当前参数:
        -chk:  CONFG_CHK=true
        -tool: 取下一个 token 作为工具名; CONFG_TOOL=true; 清空 CHK
        -git:  CONFG_GIT=true
        -design: 取下一个 token 作为参数文件名; 解析模块名; 设路径变量
        ...
        其他:  报错 exit 4
```

#### 4.1.3 源码精读

**退出码总表**集中写在帮助信息里，是整个脚本最重要的「契约」：

[dsp_rtl_lib.sh:93-103](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L93-L103) —— 这段列出了 exit 1~10 各自代表的失败类型（无 RTL 工具、无参数、帮助后退出、参数错误、源码子目录损坏、库路径错误、`-c` 与 `-t` 同用、工具链错误、拒绝覆盖旧设计、Octave 缺失）。读脚本时遇到 `exit N`，回来查这张表即可。

**无参数与帮助分支**：

[dsp_rtl_lib.sh:27-115](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L27-L115) —— 先判 `-help` 打印帮助并 `exit 3`。

[dsp_rtl_lib.sh:118-121](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L118-L121) —— 无参数则 `exit 2`。

**核心 case 循环**：脚本用一个 `until [ -z "$1" ]` 循环逐个吃 token，每个 `-xxx` 分支负责取走自己需要的额外参数（用 `shift`）并置位 `CONFG_*` 开关：

[dsp_rtl_lib.sh:122-210](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L122-L210) —— 这是参数解析主体。

其中 `-design` 分支最值得细看，它展示了「参数文件名 → 模块名」的解析套路：

[dsp_rtl_lib.sh:145-160](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L145-L160) —— 这段做四件事：① 用正则 `_[0-9].param` 从文件名 `filt_cicd_1.param` 里剥出模块名 `filt_cicd`（变量 `dsn_name`）；② 设定一组工作目录变量 `PRJ_DIR/RTL_DIR/SIM_DIR/incdir/VVP_DIR/VCD_DIR` 并 `export` 出去，后续编译命令要用。

`-demo` 分支则一次性打开三个开关（注意：帮助文本里写「demo = `-g -c -p ./ -d ...`」，但代码里 `-g`(克隆) 与 `-p`(路径) 两行被注释掉了，实际只做「检查工具 + 当前目录生成」）：

[dsp_rtl_lib.sh:176-182](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L176-L182) —— `-demo` 真正打开的是 `CONFG_DEMO`、`CONFG_CHK`、`CONFG_DSN`。所以 `-demo` **不会克隆**，你必须已经在仓库根目录里。

#### 4.1.4 代码实践

> **实践目标**：用「读帮助」和「故意触发退出码」两种方式建立对子命令的肌肉记忆。

**操作步骤**：

1. 在仓库根目录运行 `./dsp_rtl_lib.sh -h`，对照 [退出码总表](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L93-L103) 把 exit 1~10 各抄一遍，写明触发条件。
2. 故意触发几个非 0 退出码，并用 `echo $?` 观察上一个命令的退出码：
   - `./dsp_rtl_lib.sh`（无参数）→ 预期 exit **2**。
   - `./dsp_rtl_lib.sh -nosuch`（非法参数）→ 预期 exit **4**。
   - `./dsp_rtl_lib.sh -chk` → 若本机没装任何 RTL 工具，预期 exit **1**（见下方「现象」）。
3. 再跑 `./dsp_rtl_lib.sh -c -t iverilog`，预期被拒绝 → exit **7**（`-c` 与 `-t` 不能同用）。

**需要观察的现象**：

- 每次脚本都会先打印一个「HEADER MESSAGE」（带日期的 `### DATE` 横幅）。
- 触发 exit 1 时，控制台会出现 `NO RTL FRONT-END FLOW IS INSTALLED!` 红字提示。

**预期结果**：你能用「`echo $?` 打印的数字」反推出刚才发生了哪一类失败。

> 「待本地验证」：本讲义撰写环境只装了 `git`，未安装 `iverilog/vvp/octave`。我在此环境下实测 `command -v octave iverilog vvp` 均找不到（仅 `/usr/bin/git` 命中），因此运行 `-chk` 在本环境会走到 [dsp_rtl_lib.sh:340-343](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L340-L343) 这条 `exit 1` 路径。你在本机的实际退出码以你装了什么工具为准。

#### 4.1.5 小练习与答案

**练习 1**：脚本里 `-chk` 与 `-tool` 为什么不能同时使用？代码里在哪一行拦截？

答案：`-chk` 是「自动探测」，`-tool` 是「手动指定」，两者语义冲突。拦截在 [dsp_rtl_lib.sh:312-316](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L312-L316)，检测到 `CONFG_CHK` 与 `CONFG_TOOL` 同时为 true 就 `exit 7`。

**练习 2**：为什么帮助文本说 `-demo` 等价于 `-g -c -p ./ -d filt_cicd_1.param`，但实际并不会克隆仓库？

答案：因为 `-demo` 的代码分支里 `CONFG_GIT=true` 和 `CONFG_PATH=true` 两行被注释掉了（[dsp_rtl_lib.sh:176-182](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L176-L182)），实际只置位 `CONFG_DEMO/CONFG_CHK/CONFG_DSN`。帮助文本是设计意图，代码是当前实现，两者有出入——这是真实项目里常见的「文档漂移」。

---

### 4.2 .param 文件格式与 sed 参数注入

#### 4.2.1 概念说明

DRL 把「一组参数」存成一个 `.param` 文本文件，命名规则是 `<模块名>_<实例号>.param`，例如 `filt_cicd_1.param` 表示「filt_cicd 模块的第 1 号实例」。`.drl_param/` 目录下每个 Stable 模块都有一个示例 `.param`（共 8 个）。

这套机制的关键在于：**脚本不解析 Verilog，而是用 `sed` 做文本替换**。RTL 模板里每个参数声明都写着默认值，`sed` 找到「含该参数名的行」，把行内的「数字」换成 `.param` 里的值。这种做法的好处是同一套注入逻辑既能改 `.v`（RTL）也能改 `.m`（Octave GRM），保证两边参数始终一致；代价是它依赖「参数声明行的格式稳定」这一隐性约定。

#### 4.2.2 核心流程

`.param` 文件是三列、空白分隔：

```
gp_decimation_factor = 4
gp_order             = 5
...
```

第 1 列是参数名，第 2 列是 `=`，第 3 列是值。脚本用 `awk '{print $1}'` 取参数名、`awk '{print $3}'` 取值（跳过中间的 `=`），然后逐行注入。

注入流程伪代码：

```
逐行读 .param:
    param = 第1列;  value = 第3列
    sed -i '含 param 的行 { 把行内第一个数字换成 value }'  octave/stimuli.m
    sed -i '含 param 的行 { 把行内第一个数字换成 value }'  rtl/<模块>.v
```

注意一个细节：`.param` 里**没有** `gp_oup_width`。因为它是**派生参数**，在 RTL 里写成一个表达式：

\[ \texttt{gp\_oup\_width} = \texttt{gp\_inp\_width} + \texttt{gp\_order}\cdot\lceil\log_2(\texttt{gp\_decimation\_factor}\cdot\texttt{gp\_diff\_delay})\rceil \]

改了 `gp_order`，输出位宽会自动跟着变——这正是本讲综合实践要观察的现象。（CIC 位宽公式的来历留到 [u4-l3](u4-l3-cic-bitwidth-and-grm.md) 详讲。）

#### 4.2.3 源码精读

**`.param` 文件本体**：以 `filt_cicd_1.param` 为例，5 个参数、三列格式：

[.drl_param/filt_cicd_1.param:1-5](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_param/filt_cicd_1.param#L1-L5) —— 注意默认 `gp_order = 5`、`gp_decimation_factor = 4`，且不含 `gp_oup_width`（派生）。

**sed 注入的核心循环**（整条流水线最精巧、也最「魔法」的一段）：

[dsp_rtl_lib.sh:426-433](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L426-L433) —— 逐行读 `.param`，用 `awk` 切出参数名/值，然后对 `octave/stimuli.m` 和 `rtl/${dsn_name}.v` 各跑一次 `sed`。

把这条 `sed` 拆开看：

```bash
sed -i '/'"${param}"'/{s/[0-9]\+/'"${value}"'/;:a;n;ba}' rtl/${dsn_name}.v
```

- `/'"${param}"'/`：匹配「包含该参数名的行」。例如 `param=gp_order` 会命中 `parameter gp_order = 3,`。
- `s/[0-9]\+/${value}/`：把该行第一个连续数字串替换成新值（`3` → `5`）。
- `:a;n;ba`：一个 sed 读循环惯用法，用于在匹配后把剩余行排空。

**为什么不会误伤派生表达式？** `gp_oup_width` 那一行（`gp_inp_width + gp_order*$clog2(...)`）也包含 `gp_order`、`gp_inp_width` 等子串，会被匹配命中；但那一行**没有任何数字字面量**，所以 `s/[0-9]\+/.../` 无处可替换，安然无恙。这就是为什么 `.param` 只需列出「原子参数」，派生参数会自动正确。理解了这一点，你就能解释「改 `gp_order` 为什么能让输出位宽变化」——RTL 里 `gp_oup_width` 的表达式引用了被注入后的 `gp_order`。

**两侧注入目标**：同一套参数同时改 `.v` 与 `.m`，这正是「RTL 与 GRM 参数永远同步」的工程保障：

- RTL 侧 [.drl_src_code/filt_cicd/rtl/filt_cicd.v:6-11](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L6-L11)：`parameter gp_oup_width = gp_inp_width + gp_order*$clog2(...)`。
- GRM 侧 [.drl_src_code/filt_cicd/octave/stimuli.m:12](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L12)：`gp_oup_width = gp_inp_width + gp_order*ceil(log2(...));`，与 RTL 公式逐字对应。

#### 4.2.4 代码实践

> **实践目标**：亲手验证「sed 只替换含参数名行的首个数字」，并理解派生参数为何安全。

**操作步骤**：

1. 打开 [.drl_param/filt_cicd_1.param](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_param/filt_cicd_1.param#L1-L5)，记下 `gp_order = 5`。
2. 把它复制一份到临时目录模拟脚本行为（**示例代码**，不会改动源码）：
   ```bash
   # 示例代码：复现脚本的 sed 注入（在任意临时目录执行）
   param="gp_order"; value="3"
   sed '/'"${param}"'/{s/[0-9]\+/'"${value}"'/;:a;n;ba}' 你的_filt_cicd.v副本
   ```
3. 用编辑器打开注入后的 `.v`，检查：`parameter gp_order` 那行的 `5` 是否变成 `3`；`gp_oup_width` 那行是否**原封不动**。

**需要观察的现象**：

- 只有「参数声明行」的首个数字被替换。
- `gp_oup_width` 表达式行因为不含裸数字，未被改动。

**预期结果**：你会确认 sed 注入对「声明行」精确生效、对「派生表达式行」无副作用。

> 「待本地验证」：注入是文本操作，但最终位宽是否正确仍要靠仿真确认。可用 iverilog 仅做 elaborate（`iverilog -y rtl -g2012 filt_cicd.v`），观察 `gp_oup_width` 是否如公式增长。本讲义环境无 iverilog，故标待验证。

#### 4.2.5 小练习与答案

**练习 1**：`.param` 文件为什么用 `awk '{print $3}'` 取值而不是 `awk '{print $2}'`？

答案：三列分别是「参数名 / `=` / 值」。`$1`=参数名、`$2`=`=`、`$3`=值。取 `$3` 才能跳过等号拿到真正的数值（见 [dsp_rtl_lib.sh:428-429](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L428-L429)）。

**练习 2**：如果有人把 `filt_cicd_1.param` 里的 `gp_order` 行写成 `gp_order=5`（去掉空格，等号紧贴），会发生什么？

答案：`awk` 按空白分列时，`gp_order=5` 会被当成**一整列**（`$1`），`$3` 为空 → 注入的 `value` 为空，`sed` 会把目标数字替换成空串，破坏 RTL。这说明 `.param` 的「三列、有空格」格式是隐性契约，不可随意改。

---

### 4.3 design 仿真流水线：从参数到比特真回归

#### 4.3.1 概念说明

`-design`（`-d`）和 `-demo` 最终都走同一条「设计生成 + 回归仿真」流水线。这条流水线把 [u1-l1](u1-l1-project-overview.md) 讲的「三位一体」架构串成闭环：

```
.param  ──cp模板──►  工作副本  ──sed注入──►  参数化的 RTL + GRM
                                                      │
                                       Octave stimuli.m 生成
                                       ├─ stimuli_tc_N.dat   (输入激励)
                                       ├─ response_tc_N.dat  (期望输出)
                                       └─ defines_N.sv       (测试台参数宏)
                                                      │
                          每个 tc_N：符号链接 defines_N.sv → defines.sv
                                       iverilog 编译  →  vvp 仿真
                                                      │
                          测试台逐样本比对 RTL 输出 vs response → error_count
                                                      │
                                          error_count==0 ? PASSED : FAILED
```

这里最关键的认知是：**测试台本身是「哑」的**——它不知道参数是多少，参数全靠 GRM 生成的 `defines_N.sv` 宏文件注入；它也不知道「正确答案」，答案全靠 GRM 生成的 `response_tc_N.dat` 提供。RTL 与 GRM 用同一套参数，输出逐比特比对，这就是「比特真（bit-true）」验证。

#### 4.3.2 核心流程

`-design` 分支（`CONFG_DSN=true`）的执行顺序：

1. **打印参数**：`cat $file_name` 把 `.param` 内容回显到终端。
2. **覆盖保护**：若目标目录已存在，提示是否覆盖（注意：此处的 shell 判断有瑕疵，见下方源码精读）。
3. **复制模板**：`cp -rf .drl_src_code/${dsn_name}/ ${DSN_PATH}`——整棵模块模板树复制成工作副本。
4. **sed 注入参数**：对工作副本里的 `octave/stimuli.m` 与 `rtl/${dsn_name}.v` 做参数注入（见 4.2）。
5. **Octave 检查**：没装 Octave → `exit 10`（跳过验证）；装了则 `cd octave; octave --no-gui --silent stimuli.m`，生成 9 组激励/响应/defines 文件。
6. **回归循环**：`ls` 出所有 `stimuli_tc_*.dat`，对每个测试用例：
   - 符号链接 `defines_N.sv → sim/testbench/defines.sv`（sgen_nco 还额外链接两张 ROM）。
   - 把编译/仿真命令模板里的占位符 `CNT_` 替换成测试号 `N`。
   - `eval` 执行 iverilog 编译 + vvp 仿真。
7. **归档波形**：`mv *.vcd vcd`。

#### 4.3.3 源码精读

**复制模板 + 注入参数**：

[dsp_rtl_lib.sh:404-433](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L404-L433) —— `cp -rf` 复制模板，随后是上一节讲的 sed 注入循环。

> 源码阅读型发现：[dsp_rtl_lib.sh:417](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L417) 的判断写作 `if [[ y_n == n ]]`，左侧 `y_n` 没有加 `$`，在 `[[ ]]` 里会被当作字面字符串 `"y_n"` 与 `"n"` 比较，恒为假。结果是「拒绝覆盖」路径（`exit 9`）几乎不会触发，脚本默认会直接覆盖。这是一处真实的脚本瑕疵，提醒我们：读脚本不能只看意图，要看实际控制流。（行为细节「待确认」于不同 bash 版本。）

**Octave 生成「标准答案」**：

[dsp_rtl_lib.sh:436-446](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L436-L446) —— 探测 Octave，缺失则 `exit 10`；存在则 `octave --no-gui --silent stimuli.m`。

`stimuli.m` 内部对 9 个测试用例（脉冲/阶跃/斜坡/正弦/含噪正弦等）逐一调用 GRM `CICFilter`，写出三组文件：

[.drl_src_code/filt_cicd/octave/stimuli.m:97-119](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L97-L119) —— 组装 `defines` 结构体、调用 `gen_defines`、调用 `CICFilter` 得到 `yy`、分别写出 `response_tc_*_mat.dat`（期望输出）与 `stimuli_tc_*_mat.dat`（输入激励），最后 `mv` 到 `sim/testcases/` 对应子目录。

`gen_defines.m` 把参数写成测试台能 `\`define` 的宏：

[.drl_src_code/filt_cicd/octave/gen_defines.m:1-18](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/gen_defines.m#L1-L18) —— 生成 `defines_N.sv`，内含 `` `define P_DECIMATION ``、`` `define P_OUP_DATA_W ``、`` `define TESTCASE `` 等宏，并 `` `define NULL 0 ``。

**编译/仿真命令模板**（默认走 Icarus Verilog）：

[dsp_rtl_lib.sh:391-401](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L391-L401) —— `cmd_com` 用 `iverilog -y$RTL_DIR -g2012 -o ..._${dsn_name}_CNT_.vvp -DVCD ..._tb.sv`，`cmd_sim` 用 `vvp -l .../tc_CNT_.log ...vvp`。两处都保留了占位符 `CNT_`，留给回归循环替换成测试号。

**回归循环（每个测试用例一次编译+仿真）**：

[dsp_rtl_lib.sh:453-470](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L453-L470) —— 关键三步：① `ln -sf defines_$f.sv .../defines.sv` 把当前测试号的宏接到测试台；② `sed "s/CNT_/$x/g"` 把命令模板里的 `CNT_` 换成测试号；③ `eval $cmd_com_rtl; eval $cmd_sim_rtl` 真正编译并仿真。

**测试台里的逐样本比对与 PASSED/FAILED 判定**：测试台读激励（`posedge i_clk`）、读期望响应（`negedge s_clk`）、比对（`posedge s_clk`），激励文件读完即置 `data_ready`，据此判 PASS/FAIL：

[.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:63-75](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L63-L75) —— `@(posedge data_ready)` 后检查 `error_count`：>0 打印 `Testcase FAILED`，否则 `Testcase PASSED`。

[.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:97-105](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L97-L105) —— 每个 `posedge s_clk` 比对 `oup_data != o_data_mat`，不一致则 `$error` 并 `error_count++`。

测试台的参数来自 GRM 生成的宏，DUT 例化时把这些宏传进去：

[.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:107-113](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L107-L113) —— `filt_cicd #(.gp_decimation_factor(\`P_DECIMATION) ... .gp_oup_width())`，注意 `gp_oup_width` 留空，用 RTL 里那条派生表达式。

#### 4.3.4 代码实践

> **实践目标**：在装齐工具链的本机跑通 `-demo`，亲眼看到 9 个测试用例的 PASSED/FAILED；再改参数看位宽变化。

**前置条件**：本机需安装 `iverilog`、`vvp`、`octave`（`gtkwave` 可选，用来看波形）。本讲义撰写环境未安装这些（仅 `git`），故下列运行结果**待本地验证**。

**操作步骤**：

1. `cd` 到仓库根目录（`-demo` 不会克隆，必须在仓库内）。
2. 运行 `./dsp_rtl_lib.sh -demo`。
3. 观察终端：会先打印「Icarus Verilog flow is installed」（若装了）、回显 `filt_cicd_1.param` 内容、依次打印 `Simulating testcase 1..9`。
4. 每个 `stimuli_tc_*.dat` 对应一次 `iverilog` + `vvp`；最终每条 `vvp` 日志里会出现 `### INFO: Testcase PASSED`（或 FAILED）。
5. 仿真产物落在新建的 `filt_cicd/` 工作目录下：`vvp/*.vvp`、`vcd/*.vcd`、`log/tc_*.log`、`sim/testcases/{stimuli,response}/*.dat`。

**需要观察的现象 / 预期结果**（待本地验证）：

- 9 个测试用例全部打印 `Testcase PASSED`，表示 RTL 与 GRM 比特一致。
- `filt_cicd/rtl/filt_cicd.v` 里 `gp_order` 已被注入成 `.param` 的值（5）。

> 「待本地验证」：若你拿到 FAILED，最常见的两类原因是：① 工具链版本差异；② `.param` 与模板格式不符导致 sed 注入异常。先查 `log/tc_N.log`。

#### 4.3.5 小练习与答案

**练习 1**：回归循环里为什么要把 `CNT_` 替换成测试号 `N`，而不是直接为每个测试用例写一条独立的编译命令？

答案：因为 9 个测试用例的 RTL **完全相同**，唯一不同的是「读哪个 `defines_N.sv` / 哪组 `.dat`」。用占位符 `CNT_` 复用同一条命令模板（[dsp_rtl_lib.sh:391-401](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L391-L401)），在循环里 `sed` 替换（[dsp_rtl_lib.sh:465-466](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L465-L466)），既避免重复定义又让每个用例有独立的 `.vvp`/`.log`/`.vcd`。

**练习 2**：测试台里 `error_count` 在哪个时钟沿自增？判定 PASSED/FAILED 又由谁触发？

答案：`error_count` 在 `posedge s_clk` 比对不一致时自增（[filt_cicd_tb.sv:97-105](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L97-L105)）；激励文件读完时 `data_ready` 置 1，`@(posedge data_ready)` 触发最终的 PASSED/FAILED 打印（[filt_cicd_tb.sv:63-75](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L63-L75)）。

## 5. 综合实践

**任务**：用「改一个参数 → 看位宽变化」把本讲三个模块串起来验证一遍。

1. 复制一份 `.drl_param/filt_cicd_1.param` 到工作副本（或直接 `-d` 生成）。当前 `gp_order = 5`、`gp_decimation_factor = 4`、`gp_inp_width = 8`。
2. 用 CIC 位宽公式手算 `gp_oup_width`：
   - `gp_order=5`：\( 8 + 5\cdot\lceil\log_2(4\cdot1)\rceil = 8 + 5\cdot2 = 18 \)
   - 把 `.param` 里 `gp_order` 改成 `3`：\( 8 + 3\cdot2 = 14 \)
3. 重新 `./dsp_rtl_lib.sh -d filt_cicd_1.param`（或 `-demo`），确认：
   - sed 把 `rtl/filt_cicd.v` 与 `octave/stimuli.m` 里的 `gp_order` 都从 5 改成 3；
   - `defines_N.sv` 里 `` `define P_OUP_DATA_W `` 随之从 18 变成 14；
   - 回归仿真依旧 9 例 PASSED（因为 RTL 与 GRM 同步变小，仍是比特真）。
4. 打开任一 `vcd/*.vcd`（用 gtkwave），确认 `o_data` 的位宽与上面手算一致。

> 「待本地验证」：步骤 3、4 需 `iverilog`/`octave`/`gtkwave`。手算（步骤 2）可立即完成，是检查理解的最好方式。

**交付物**：一张表，记录 `gp_order ∈ {3, 5, 8}` 各自的 `gp_oup_width`（手算）与仿真实测是否一致。

## 6. 本讲小结

- `dsp_rtl_lib.sh` 是 DRL 唯一入口，用 `case` 循环解析 `-chk/-git/-d/-s/-demo/-dev` 等子命令，每种失败对应一个专属退出码（1~10）。
- `.param` 是「三列、键 = 值」格式；脚本用 `awk` 取参数名/值，再用 `sed` 把值「文本替换」进 RTL 和 Octave GRM，保证两侧参数同步。
- `gp_oup_width` 是派生参数（不在 `.param` 里），改 `gp_order` 会通过表达式自动改变输出位宽。
- `-design`/`-demo` 走完整流水线：复制模板 → sed 注入 → Octave 生成激励/响应/defines → 回归循环逐测试用例编译仿真。
- 比特真验证的闭环是：测试台读 GRM 的 `response_tc_*.dat` 当标准答案，在 `s_clk` 沿逐样本比对 RTL 输出，`error_count==0` 即 PASSED。
- 读脚本要区分「帮助文本的意图」与「代码的实际行为」（如 `-demo` 实际不克隆、覆盖判断有 `$` 瑕疵）。

## 7. 下一步学习建议

- 想深入「时序约定与接口规范」→ 下一讲 [u1-l4 统一编码风格与接口约定](u1-l4-coding-style-and-interface.md)，以 `dff.v` 为范本讲清 `i_rst_an/i_ena/i_clk` 与 `gp_/c_/r_/w_` 命名。
- 想深入「验证方法学」→ [u7-l1 比特真验证方法论](u7-l1-bittrue-verification.md) 会逐行精读测试台的 TEXTIO 与比对节拍；[u7-l2 九测试用例激励设计模式](u7-l2-testcase-stimuli-pattern.md) 详讲 `stimuli.m` 的 9 个用例。
- 想自己造一个模块 → [u7-l3 dev 模式 — 脚手架创建新模块](u7-l3-dev-mode-scaffolding.md) 讲 `-dev` 如何用 heredoc 生成 RTL/TB 模板。
- 建议继续通读 `dsp_rtl_lib.sh` 全文一遍，重点把 9 个 `exit` 点和 3 个 `CONFG_*` 主开关（CHK/DSN/DEV）在脑中连成图。
