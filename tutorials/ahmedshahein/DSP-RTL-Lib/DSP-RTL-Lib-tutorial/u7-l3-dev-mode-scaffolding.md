# dev 模式 — 脚手架创建新模块

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `./dsp_rtl_lib.sh -dev` 一键生成一个符合 DRL 目录约定的新模块骨架（RTL 模板 + 测试台模板 + 标准子目录）。
- 说清楚 `-dev` 的三个参数 `-design` / `-folder` / `-author` 如何被解析、如何校验，以及校验逻辑的薄弱点。
- 看懂脚本用 bash **heredoc**（`cat <<EOF`）把整份 RTL 与 SystemVerilog 测试台模板「就地打印」出来的机制，并理解模板如何复用全库统一的时序约定与 TEXTIO 验证套路。
- 识别本版本脚手架里的两处真实瑕疵（测试台里 `AMPd0` 未被替换、`-help` 文本与代码不一致），并能自己修补。
- 以脚手架为起点，二次开发一个自定义 DSP 模块（如滑动平均滤波器）。

## 2. 前置知识

本讲是单元 7（验证方法学与工程实践）的收尾篇，承接以下已建立的知识，不再重复：

- **u1-l3 工具链与构建运行流程**：`dsp_rtl_lib.sh` 是全库唯一入口，用 `case` 循环解析子命令、置位 `CONFG_*` 开关；`.param` 用 `sed` 注入参数；`-d` 流程跑「复制模板 → sed 注入 → GRM 生成激励/响应 → iverilog 回归」。本讲的 `-dev` 与 `-d` 是**两条互不相干**的分支：`-d` 从已有模板「现场生成」一个可仿真实例，`-dev` 则「凭空创建」一个全新的、空白的模块骨架供你二次开发。
- **u1-l2 仓库结构与目录组织**：每个模块都遵循 `rtl / sim/{testbench, testcases/{stimuli,response}} / octave / log` 的标准子目录约定，由脚本里的 `SUB_FOLDERS` 数组定义并做完整性自检。`-dev` 要做的，就是把这套目录结构复制给一个新模块。
- **u1-l4 统一编码风格与接口约定**：异步低有效复位 `i_rst_an`、同步高有效使能 `i_ena`、上升沿触发；命名前缀 `gp_`(参数)/`c_`(常量)/`r_`(寄存器)/`w_`(线网)。脚手架生成的 RTL 模板**严格遵循**这套约定。
- **u7-l1 比特真验证方法论**：测试台是「哑」的，靠 TEXTIO（`$fscanf`）在 `i_clk` 沿喂激励、在 `s_clk`（=`dut.w_sclk`）沿逐样本比对，`error_count` 判定 PASSED/FAILED。脚手架生成的 TB 模板就是这套套路的浓缩版。

此外需要一点 **bash 基础**：heredoc（here-document）是一种把多行文本「原样」塞给命令的语法，形如 `cat <<EOF ... EOF`，两个 `EOF` 之间的所有内容会逐字写入文件；其中以 `$` 开头的变量会被展开。

## 3. 本讲源码地图

本讲只涉及**一个**源码文件，但它是全库唯一的可执行入口：

| 文件 | 作用 | 本讲关注范围 |
| --- | --- | --- |
| `dsp_rtl_lib.sh` | 全库命令行入口，含克隆、检查、设计、仿真、开发五大模式 | `-dev` 分支：参数解析(L183-203)、模式闸门(L499)、参数校验(L501-508)、两个 heredoc 模板函数(L511-684)、目录脚手架(L686-747) |

为对照「脚手架模板」与「真实模块」，还会引用一个真实测试台作为参照：

| 文件 | 作用 |
| --- | --- |
| `.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv` | 真实的 CIC 抽取滤波器测试台，用于对比脚手架生成的 TB 模板，定位瑕疵 |

## 4. 核心概念与源码讲解

### 4.1 `-dev` 参数解析与校验

#### 4.1.1 概念说明

`-dev`（也可写 `-v`）是 DRL 的「**新模块开发模式**」：它不读取 `.param`、不调用 GRM、不跑仿真，只做一件事——按你的命名，在当前目录下「打印」出一个空白模块骨架。这是把 DRL 当作**二次开发脚手架**（scaffold）来用的起点：你想加一个库里没有的 DSP 模块（比如滑动平均、IIR、直流消除），就从 `-dev` 开始。

值得注意的是：**README 完全没有提及 `-dev`**，它只在 `-help` 输出里有文档。所以这是一个「隐藏」功能，只能靠读 `-help` 或读源码发现。

#### 4.1.2 核心流程

`-dev` 在脚本里的生命周期分为四步：

1. **外层 `case` 识别 `-dev | -v`**，进入一个**嵌套的 `until` 循环**专门消费它的三个子参数。
2. 嵌套循环用一个 `case` 把 `-design` / `-author` / `-folder` 的值分别存入 `MODULE_NAME` / `AUTHOR` / `FOLDER_NAME`，然后置位 `CONFG_DEV=true`。
3. 脚本继续往后走（跳过 `-d`、`-s` 等无关分支），直到 L499 的模式闸门 `if [ "$CONFG_DEV" = "true" ]`。
4. 闸门内先做参数校验，校验通过后调用模板函数、创建目录、落盘文件。

伪代码：

```text
case "-dev":
    进入嵌套 until 循环:
        "-design" -> MODULE_NAME = 下一个参数
        "-author" -> AUTHOR      = 下一个参数
        "-folder" -> FOLDER_NAME = 下一个参数
    CONFG_DEV = true

... (跳过其它分支) ...

if CONFG_DEV:
    if (三个参数都为空):  打印错误用法
    else:
        定义 create_rtl_file / create_testbench 两个函数
        创建标准目录树
        在 rtl/ 落盘 MODULE_NAME.v
        在 sim/testbench/ 落盘 MODULE_NAME_tb.sv
```

#### 4.1.3 源码精读

**参数解析**（嵌套 `until` + `case`）：[dsp_rtl_lib.sh:183-203](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L183-L203)

```bash
"-dev" | "-v")
  shift;
  until [ -z "$1" ]; do
    case $1 in
      "-design") shift; MODULE_NAME=$1 ;;
      "-author") shift; AUTHOR=$1 ;;
      "-folder") shift; FOLDER_NAME=$1 ;;
    esac
    shift
  done
  CONFG_DEV=true
  ;;
```

关键点：每个分支里先 `shift` 跳过标志本身、再用 `$1` 取它的值；循环末尾再 `shift` 一个，逐步消化整条命令行。这与外层 `-d`/`-s` 的「`shift` 一次取一个值」写法不同，因为 `-dev` 的三个参数**顺序无关**、各自带标志。

**`-help` 文档**（仅此处可见）：[dsp_rtl_lib.sh:88-91](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L88-L91) —— 注意帮助文本写的是 `-design_name`，而代码实际解析的是 `-design`，这是一处**文档漂移**，照帮助文本敲 `-design_name` 会失败。

**模式闸门与校验**：[dsp_rtl_lib.sh:499-508](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L499-L508)

```bash
if [ "$CONFG_DEV" = "true" ]
then
  if [[ -z $MODULE_NAME && -z $AUTHOR && -z $FOLDER_NAME ]];
  then
    echo "###ERROR : Wrong number of arrguments for development mode."
    ... (打印用法示例) ...
  else
    ... (脚手架逻辑) ...
```

⚠️ **校验薄弱点**：判定条件用 `&&`（与），意味着**只有三个参数全为空**才报错。如果你只给了 `-design filt_avg`、漏了 `-author` 和 `-folder`，条件为假，脚本照样进入 `else` 执行脚手架——此时 `FOLDER_NAME` 为空，会导致目录被创建在**当前工作目录（仓库根）**而不是子文件夹里（见 4.3）。而且这个错误分支**只 `echo`、不 `exit`**，不会中断。结论：**请务必一次性传齐三个参数**。

#### 4.1.4 代码实践

1. **实践目标**：亲手触发 `-dev`，观察参数如何变成目录与文件；并验证校验的薄弱点。
2. **操作步骤**：
   - 在仓库根执行：
     ```bash
     ./dsp_rtl_lib.sh -dev -author "Your Name" -folder "filt_avg" -design "filt_avg"
     ```
   - 再**故意**只传一个参数，观察行为：
     ```bash
     ./dsp_rtl_lib.sh -dev -design "filt_avg2"
     ```
3. **需要观察的现象**：第一次命令应打印 `Development Mode.` 并在 `filt_avg/` 下生成目录与模板文件（详见 4.2、4.3）；第二次命令不会报错退出，而是尝试在**当前目录**直接创建 `rtl/`、`sim/` 等（污染仓库根）。
4. **预期结果**：第一次成功生成 `filt_avg/rtl/filt_avg.v` 与 `filt_avg/sim/testbench/filt_avg_tb.sv`；第二次因 `FOLDER_NAME` 为空而把目录散落在仓库根。
5. **待本地验证**：以上为依据源码静态分析的预期。请在**带 git 的副本**里试验，试验后用 `git status` 与 `git clean -ndx` 清理生成的文件，避免污染仓库；第二次（错误用法）尤其建议在一个临时空目录里做。

> 提示：本环境无法替你执行该脚本（运行需授权），故仿真与生成结果标注为「待本地验证」。后续实践同此说明。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `-dev` 用嵌套 `until` 循环解析参数，而 `-d` 只用一次 `shift`？
  - **答案**：`-d` 后面只跟一个值（`.param` 文件名），一次 `shift` 即可；`-dev` 后面跟着**三个顺序无关、各自带标志**的参数（`-design`/`-author`/`-folder`），需要循环逐个识别标志再取值。
- **练习 2**：如果把校验里的 `&&` 改成 `||`（或），行为会如何变化？
  - **答案**：改成 `||` 后，**只要任一参数为空**就报错，即「三个都必须非空」才是合法用法，这才是更稳健的校验。当前 `&&` 版本只在「三者全空」时报错，留下「部分传参」的漏洞。

### 4.2 heredoc RTL/TB 模板生成

#### 4.2.1 概念说明

校验通过后，脚本用 bash 的 **heredoc**（`cat <<EOF ... EOF`）把两份「模板源码」逐字打印成文件。这种做法的好处是：**把模板直接嵌在脚本里**，不依赖外部模板文件，一个 `.sh` 就能分发整个脚手架能力。两份模板分别由两个 bash 函数产出：

- `create_rtl_file` → 生成可综合 RTL 骨架 `$MODULE_NAME.v`；
- `create_testbench` → 生成 SystemVerilog 测试台骨架 `${MODULE_NAME}_tb.sv`。

模板里凡是模块名、作者名出现的地方，都用 `$MODULE_NAME` / `$AUTHOR` 变量占位，heredoc 在打印时自动展开——所以一份模板能服务任意命名的新模块。

#### 4.2.2 核心流程

两份模板的设计意图：

| 模板 | 嵌入的 DRL 约定 |
| --- | --- |
| RTL（`$MODULE_NAME.v`） | 版权头 `$AUTHOR`；模块名 `$MODULE_NAME`；参数 `gp_inp_width/gp_oup_width`；标准端口 `i_rst_an/i_ena/i_clk/i_data/o_data`；`localparam c_`、`reg r_`、`wire w_` 声明占位；三段式 `always @(posedge i_clk or negedge i_rst_an)`（复位/使能）；组合 `always @(*)` + `case`；`` `include `` 钩子 |
| 测试台（`_tb.sv`） | `` `timescale ``、`` `include "defines.sv" ``；时钟/复位/使能波形生成；TEXTIO：`$fscanf` 读激励与响应、`s_clk = dut.w_sclk` 对齐慢时钟、`error_count`、`data_ready`（EOF 触发）、PASSED/FAILED 判定；DUT 例化骨架 |

测试台模板里有意用了一些**占位串**（`AMPb`、`AMPd`），是因为 heredoc 会把 `$`、反引号等特殊字符当 bash 语法处理；为了在生成的 SV 文件里得到字面的 `'b1`、`'d0`（Verilog 二进制/十进制字面量），模板先写成 `1AMPb1`、`AMPd0`，打印完再用 `sed` 把 `AMPb` 还原成 `'b`。这是一种**绕过 heredoc 转义**的实用技巧。

#### 4.2.3 源码精读

**RTL 模板函数**（heredoc 打印 RTL 骨架）：[dsp_rtl_lib.sh:511-562](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L511-L562)。摘关键段：

```verilog
module $MODULE_NAME #(
  parameter gp_inp_width   = 8,  // ROM address bit-width
  parameter gp_oup_width   = 16
) (
  input  wire                           i_rst_an,
  input  wire                           i_ena,
  input  wire                           i_clk,
  input  wire        [gp_inp_width-1:0] i_data,
  output wire signed [gp_oup_width-1:0] o_data
);
  ...
  always @(posedge i_clk or negedge i_rst_an)
    begin: p_<xxx>
      if (!i_rst_an)       begin end   // 复位
      else if (i_ena)      begin end   // 使能采样
    end
```

这正是 u1-l4 讲过的「异步低有效复位 + 同步高有效使能 + 上升沿」三段式骨架，`xxx` 是留给你的占位符。注意 heredoc 用的是 `<<EOF`（非 `<<-EOF`），所以**模板里每行的前导缩进会被原样保留**进生成的 `.v`——这无害（Verilog 不在乎缩进），但你打开生成的文件会看到统一的 4 空格缩进。

**测试台模板函数**（heredoc 打印 TB 骨架 + 占位串）：[dsp_rtl_lib.sh:564-684](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L564-L684)。关键占位串出现处：[dsp_rtl_lib.sh:597-608](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L597-L608)（复位/使能/时钟初始化用 `1AMPb1`/`1AMPb0`）、[dsp_rtl_lib.sh:647](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L647)（`i_data = AMPd0;`）、[dsp_rtl_lib.sh:650](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L650)（`data_ready = 1AMPb1;`）。

**占位串还原**（仅替换 `AMPb`）：[dsp_rtl_lib.sh:683](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L683)

```bash
sed -i "s/AMPb/'b/g" $MODULE_NAME\_tb.sv
```

⚠️ **真实瑕疵①（会导致测试台无法编译）**：这条 `sed` 只把 `AMPb` 还原成 `'b`，却**没有**把 `AMPd` 还原成 `'d`。对照真实模块 `.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:82`（`i_data = 'd0;`）可知，[dsp_rtl_lib.sh:647](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L647) 的 `i_data = AMPd0;` 本意是生成 `i_data = 'd0;`，但还原步骤漏了它。于是**脚手架生成的测试台里会留下字面量 `AMPd0`，编译时报错**。修补办法是在 L683 后补一条：

```bash
sed -i "s/AMPd/'d/g" $MODULE_NAME\_tb.sv
```

⚠️ **真实瑕疵②（仅提示文本）**：[dsp_rtl_lib.sh:742](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L742) 的 `echo` 说生成的文件是 `${MODULE_NAME}_tb.v`，但 heredoc 实际写的是 `.sv`（[dsp_rtl_lib.sh:565](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L565)）。文件后缀是 `.sv`，提示文本里的 `.v` 是笔误，不影响功能。

#### 4.2.4 代码实践

1. **实践目标**：查看脚手架生成的两份模板文件，定位并修补 `AMPd0` 瑕疵。
2. **操作步骤**：
   - 先按 4.1.4 生成 `filt_avg/`。
   - 打开两份文件观察：
     ```bash
     cat filt_avg/rtl/filt_avg.v
     cat filt_avg/sim/testbench/filt_avg_tb.sv
     ```
   - 在 TB 里搜索 `AMPd0`：能找到 `i_data = AMPd0;`，确认瑕疵存在。
   - 手动修补：把该行改成 `i_data = 'd0;`（或先修脚本 L683 再重新生成）。
   - 与真实模块对比：`diff` 生成的 TB 与 `.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv`，列出脚手架版「缺了什么」（如 `s_clk` 用 `initial assign` 而真实版用 `assign #1`、缺 `VCD` 段、DUT 例化端口 `.i_data()` 留空等）。
3. **需要观察的现象**：生成的 RTL 含 `$MODULE_NAME`/`$AUTHOR` 已被替换为 `filt_avg`/`Your Name`；TB 里 `1AMPb1` 已变 `1'b1`，但 `AMPd0` 原样残留。
4. **预期结果**：修补前 TB 无法通过 iverilog 编译（`AMPd0` 是未知词法记号）；修补 `AMPd0 → 'd0` 后该语法错误消失（但要让仿真真正跑通，还需补齐 DUT 端口连线和 `.dat` 激励/响应，见第 5 节综合实践）。
5. **待本地验证**：编译是否报错、修补后是否通过，需本地用 iverilog 验证。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 TB 模板要用 `AMPb`/`AMPd` 占位串，而不是直接写 `'b1`/`'d0`？
  - **答案**：heredoc（`<<EOF`，未加引号）会对内容做变量展开和命令替换。直接写 `'b`、`'d` 虽然不触发 `$` 展开，但作者用 `AMPb`/`AMPd` 这类显眼占位串是为了**完全避开** heredoc 对各种特殊字符的处理、并让事后 `sed` 还原更可控（一眼能搜到）。本质是「先写安全占位符、打印后再批量替换」的转义绕过技巧。
- **练习 2**：除了补 `AMPd→'d` 的 sed，TB 模板还有哪些地方需要你手工补全才能仿真？
  - **答案**：DUT 例化的端口 `.i_data()` 与参数 `.gp_inp_width()/` 都留空，需补上连线；TB 缺少 VCD dump 段（可选）；最关键的是**没有 `.dat` 激励/响应文件与 `defines.sv` 宏文件**——这些要靠 octave GRM（`stimuli.m`/`gen_defines.m`）生成，脚手架并不替你做。

### 4.3 标准目录脚手架

#### 4.3.1 概念说明

`-dev` 的第三块职责是**复刻 DRL 的标准模块目录结构**。u1-l2 讲过，全库 8 个模块都遵循同一套子目录约定（由 `SUB_FOLDERS` 数组定义、克隆后做完整性自检）。`-dev` 把这套结构 mkdir 给新模块，使其**一出生就与 `-d`/`-s` 流程兼容**——你可以接着用 `./dsp_rtl_lib.sh -s filt_avg 1` 这样的命令去仿真它（前提是你补齐了 octave GRM 等内容）。

#### 4.3.2 核心流程

目录创建分两条路径，取决于是否传了 `-folder`：

```text
if FOLDER_NAME 非空:
    if 该文件夹不存在:
        mkdir FOLDER_NAME; cd 进去
        mkdir -p rtl
        mkdir -p sim/testbench  sim/testcases/stimuli  sim/testcases/response
        mkdir -p octave
        mkdir -p log
        cd 回上级
else (FOLDER_NAME 为空):
    在当前目录直接 mkdir 同样的子目录   ← 4.1 警告的「污染仓库根」路径
```

目录建好后，再分别进入 `FOLDER_NAME/rtl` 与 `FOLDER_NAME/sim/testbench`，调用 4.2 的两个函数落盘模板文件。两处落盘前都加了**存在性守卫**（`if [ -z "$(ls -A 目录)" ]`：目录为空才生成），避免覆盖你已有的文件。

#### 4.3.3 源码精读

**标准子目录定义**（对照基准）：[dsp_rtl_lib.sh:261-268](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L261-L268)

```bash
SUB_FOLDERS=(\
"rtl" \
"sim/testcases/stimuli" \
"sim/testcases/response" \
"sim/testbench" \
"octave" \
"log" \
)
```

**`-dev` 的目录创建**（与上完全对齐）：[dsp_rtl_lib.sh:689-728](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L689-L728) —— 含 `if/else` 两条路径。

**带守卫的文件落盘**：[dsp_rtl_lib.sh:730-747](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L730-L747)

```bash
if [ -z "$(ls -A $FOLDER_NAME/rtl)" ]; then
  cd $FOLDER_NAME/rtl
  echo "... Creating RTL file $MODULE_NAME.v at $FOLDER_NAME/rtl"
  create_rtl_file
  cd ../../
else
  echo "### WARNING: RTL file already exists"
fi
```

对照结论：脚手架生成的子目录集合 = `SUB_FOLDERS`，完全一致。但脚手架**不会**生成：`.param` 文件、octave 下的 `stimuli.m`/`gen_defines.m`/`quantize.m`/GRM、`empty_file` 占位文件、`vvp/`/`vcd/`（后两者本就是 `-d` 流程运行时才建的产物）。这些都要你二次开发时参考已有模块（如 `filt_cicd`）手工补齐。

#### 4.3.4 代码实践

1. **实践目标**：确认脚手架目录结构与全库标准一致，并清单出「还缺什么」。
2. **操作步骤**：
   - 生成 `filt_avg/` 后打印目录树：
     ```bash
     find filt_avg -type d | sort
     find filt_avg -type f | sort
     ```
   - 与标准结构对照：`rtl`、`sim/testbench`、`sim/testcases/stimuli`、`sim/testcases/response`、`octave`、`log` 是否齐备。
   - 与真实模块对照：`find .drl_src_code/filt_cicd -type f | sort`，列出 `filt_avg` 相比 `filt_cicd` 缺少的文件类别。
3. **需要观察的现象**：`filt_avg` 下 6 个标准子目录齐备；`rtl/filt_avg.v` 与 `sim/testbench/filt_avg_tb.sv` 已生成；`octave/`、`log/`、`sim/testcases/{stimuli,response}/` 为空。
4. **预期结果**：目录结构 100% 对齐 `SUB_FOLDERS`；缺失的是 octave GRM 一整套脚本、`.param`、`empty_file` 占位符。
5. **待本地验证**：目录是否如预期生成，需本地执行确认。

#### 4.3.5 小练习与答案

- **练习 1**：为什么落盘模板前要加 `if [ -z "$(ls -A 目录)" ]` 守卫？
  - **答案**：防止 `-dev` 覆盖你已经写好的代码。`ls -A` 列出目录下所有（含隐藏）文件，为空才进 `create_*` 函数；非空则只打印 `WARNING: ... already exists`。这让脚手架「**只填空、不破坏**」，可安全反复运行。
- **练习 2**：脚手架建了 `octave/` 空目录，却不生成任何 `.m` 文件。要让新模块具备比特真验证能力，你需要在 `octave/` 下补哪些脚本？
  - **答案**：至少要 `stimuli.m`（生成激励/响应 `.dat` + 调用 `gen_defines`）、`gen_defines.m`（产出 `defines_<tc>.sv` 宏）、`quantize.m`/`quantizer.m`（定点量化），以及该模块的黄金参考模型（GRM，如 CIC 用 `CICFilter.m`、FIR 用 `gen_coeffs.m`）。这些都能从 `filt_cicd/octave/` 等已有模块拷贝改写。

## 5. 综合实践

把本讲三块知识串起来，从零造一个**滑动平均（boxcar）滤波器** `filt_avg`。该实践分三个递进难度，建议至少完成第一层。

**背景**：滑动平均是最简单的 FIR——长度 L 的窗内系数全为 1（或 1/L）。取 L=4、不做归一化时：

\[
y[n] = x[n] + x[n-1] + x[n-2] + x[n-3]
\]

对单位脉冲 \(x=[1,0,0,0,\dots]\)，输出为 \([1,1,1,1,0,\dots]\)（4 个 1 后归零），这正是「4 抽头矩形窗」的脉冲响应。

### 第一层（核心，直接练习 `-dev`，必做）

1. 在仓库副本里执行：
   ```bash
   ./dsp_rtl_lib.sh -dev -author "Your Name" -folder "filt_avg" -design "filt_avg"
   ```
2. 用 `find filt_avg` 确认目录树与两份模板文件已生成（验证 4.3）。
3. 打开 `filt_avg/sim/testbench/filt_avg_tb.sv`，定位 `i_data = AMPd0;`，手工改成 `i_data = 'd0;`（修补 4.2 瑕疵①）。
4. 打开 `filt_avg/rtl/filt_avg.v`，确认版权头是 `Copyright (C) 2019 Your Name`、模块名是 `filt_avg`，验证 heredoc 变量展开正确。

### 第二层（实现 RTL，进阶）

把 `filt_avg.v` 的模板填充为一个 4 抽头滑动平均（复用 u2-l2 的 `dff` 做延迟线，或用 `shift_register`）。要点：

- 用 3 个 `dff` 串成延迟线，得到 `x[n-1]`、`x[n-2]`、`x[n-3]`；
- 组合求和 `assign o_data = i_data + d1 + d2 + d3;`（注意位宽：4 个 `gp_inp_width` 位数相加需 `+⌈log₂4⌉=+2` 位，参考 u2-l1）；
- 把 `gp_oup_width` 设为 `gp_inp_width + 2`。

### 第三层（跑通仿真，挑战）

脚手架的 TB 依赖 octave GRM 产出的 `.dat` 与 `defines.sv`，而脚手架不生成它们。两条路：

- **简捷路（手搓激励）**：在 `filt_avg/sim/testcases/stimuli/` 手写 `stimuli_tc_1_mat.dat`（放一个 `1` 后跟一串 `0`，模拟脉冲），在 `response/` 手写 `response_tc_1_mat.dat`（按脉冲响应填 `1 1 1 1 0 ...`），再手写一个最小 `defines_1.sv`（定义 `P_INP_DATA_W`、`P_OUP_DATA_W`、`TESTCASE`、`NULL`），并按 u1-l3 的命令模式用 iverilog 编译仿真：
  ```bash
  cd filt_avg && mkdir -p vvp vcd
  ln -sf ../sim/testcases/stimuli/defines_1.sv ../sim/testbench/defines.sv
  iverilog -y rtl -Irtl -Isim/testbench -g2012 -o vvp/filt_avg_1.vvp -DVCD sim/testbench/filt_avg_tb.sv
  vvp -l log/tc_1.log vvp/filt_avg_1.vvp
  ```
- **正规路**：仿照 `filt_cicd/octave/` 写一整套 `stimuli.m` + `gen_defines.m` + GRM，再走 `-d`/`-s` 流程。

**预期结果与待本地验证**：脉冲激励下，RTL 输出应在 4 拍内逐拍给出 `1`、之后归零，与手写的 `response_tc_1_mat.dat` 逐样本相等，测试台打印 `Testcase PASSED`。本环境无法执行仿真，以上为基于算法与测试台逻辑的预期，**请本地验证**；若 TB 模板的 DUT 例化端口（`.i_data()` 留空）未补全，需先补连线再编译。

## 6. 本讲小结

- `-dev`（或 `-v`）是 DRL 的「新模块开发模式」，**README 未记载**，仅在 `-help` 中有文档；它与 `-d` 互不相干——`-d` 现场生成可仿真实例，`-dev` 凭空打印空白骨架。
- 参数靠**嵌套 `until` + `case`** 解析 `-design`/`-author`/`-folder` 三个顺序无关的子参数；校验用 `&&`（三者全空才报错）且报错不 `exit`，属薄弱设计，务必一次传齐三参。
- 两份模板由 bash **heredoc** 就地打印：RTL 模板严格复用全库时序与命名约定，TB 模板复用 TEXTIO 比特真套路，并用 `AMPb`/`AMPd` 占位串绕过 heredoc 转义、事后 `sed` 还原。
- 本版本有两处真实瑕疵：① `sed` 只还原 `AMPb→'b`、漏了 `AMPd→'d`，导致生成的 TB 含非法词法 `AMPd0` 无法编译；② `echo` 提示文件后缀误写 `.v`（实为 `.sv`）。
- 目录脚手架复刻的子目录集合与 `SUB_FOLDERS` 完全一致（`rtl/sim/octave/log` 等），并带「只填空不覆盖」的 `ls -A` 守卫；但不生成 `.param`、octave GRM、`empty_file`，需二次开发补齐。
- 二次开发的正确姿势：`-dev` 造骨架 → 修瑕疵 → 填 RTL → 补 octave GRM/`.dat`/`defines.sv` → 用 `-d`/`-s` 或直接 iverilog 跑比特真回归。

## 7. 下一步学习建议

本讲是单元 7 也是整本手册的最后一篇，你已掌握从「跑通 demo」到「自己造模块」的完整链条。后续建议：

- **动手做一个真实模块**：用本讲的滑动平均 `filt_avg` 走完整流程（含 octave GRM），把它当作毕业项目；可进阶尝试 IIR（README 列为 Planned）或直流消除（DC offset cancellation）。
- **重温验证闭环**：若你对脚手架 TB 的 TEXTIO 细节还有疑问，回看 u7-l1（比特真方法论）与 u7-l2（九测试用例激励设计），它们给出了补齐 `stimuli.m`/`gen_defines.m` 的完整范本。
- **深入研究已有模块**：从最简单的 `filt_fir`（u3）与 `filt_cicd`（u4）开始，对照它们的 `octave/` 目录，理解一个「成熟的 DRL 模块」应该长什么样——这是你二次开发的最佳模板。
- **关注上游**：DRL 的 RTL 目前是「常规实现、不做面积/功耗优化」（见 README）。如果你对面积/功耗/时序优化感兴趣，可在此基础上探索资源共享（参考 `filt_mac`）、多相分解（参考 `filt_ppd`）等以面积换吞吐的架构，这正是手册单元 3–5 的主题。
