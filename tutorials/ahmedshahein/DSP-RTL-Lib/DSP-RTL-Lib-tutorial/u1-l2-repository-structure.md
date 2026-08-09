# 仓库结构与目录组织

## 1. 本讲目标

上一讲我们认识了 DSP-RTL-Lib（DRL）「三位一体」的工程哲学：每个 DSP 模块同时拥有可综合 RTL、SystemVerilog 测试台、Octave 黄金参考模型。本讲要回答一个更落地的问题：**这些东西到底分别放在仓库的哪里？**

读完本讲，你应当能够：

1. 一眼看懂 DRL 仓库的顶层布局——哪些目录是「源码模板库」、哪些是「参数库」、哪些是构建脚本与手册。
2. 说出任何一个模块内部 `log / octave / rtl / sim / vcd / vvp` 这套标准子目录各自存放什么、何时被填充。
3. 区分清楚「源码模板」与「构建产物」——哪些文件是 git 跟踪、提交进仓库的，哪些是你在本地跑完一次设计/仿真后才生成出来的。这是后续读懂构建流水线（下一讲）的地基。

## 2. 前置知识

本讲假设你已经读过 [u1-l1 项目总览](u1-l1-project-overview.md)，知道以下名词：

- **RTL（Register Transfer Logic）**：用 Verilog 描述的、可被综合成真实电路（ASIC/FPGA）的硬件代码。DRL 用的是 Verilog 2001。
- **测试台（Testbench, TB）**：用来给 RTL 喂输入、检查输出的「驱动程序」。DRL 用 SystemVerilog 2012 写测试台。
- **GRM（Golden Reference Model，黄金参考模型）**：用 Octave（开源版 MATLAB）写的浮点/定点参考实现，作用是产出「标准答案」，让测试台逐比特比对 RTL 输出。比特一致即称「比特真（bit-true）」。
- **M&M（mix-and-match，混搭）**：把系统拆成基本组件，挑选、参数化、拼装成完整系统的设计理念。

另外需要两个通用的工程小知识：

- **git 不跟踪空目录**。一个目录里如果没有任何文件，`git add` 不会把它纳入版本库。DRL 用一个名为 `empty_file` 的占位文件来「撑住」那些暂时为空、要等构建后才填充的目录。
- **`.gitignore`**：一个文本文件，逐行列出「不想被 git 跟踪」的文件名模式（如 `*.log`、`*.vcd`），用来把构建产物排除在版本库之外。

## 3. 本讲源码地图

本讲只读两个文件，但它们定义了整个仓库的「骨架契约」：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md) | 项目的说明书，其中 `Folder Structure` 一节用 `filt_ppd` 为例画出了模块的标准目录树。 |
| [dsp_rtl_lib.sh](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh) | 唯一的构建脚本（Bash）。它内部用两个数组 `FOLDERS` / `SUB_FOLDERS` 定义了「源码模板库该长什么样」，并在设计/仿真流程里把模板复制成可运行的设计目录。 |

我们还会顺带用 `ls` 与 `git ls-files` 对照仓库的真实磁盘内容（不是源码，是验证依据）。

## 4. 核心概念与源码讲解

### 4.1 顶层目录布局

#### 4.1.1 概念说明

DRL 的仓库根目录下，文件并不多，但每一项都有明确的分工。可以把顶层分成三类：

1. **入口与文档**：`README.md`（说明）、`LICENSE`（BSD 2-Clause 许可）、`dsp_rtl_lib.pdf`（PDF 版手册）。
2. **构建脚本**：`dsp_rtl_lib.sh`——整个项目唯一的「操作面板」，所有设计、仿真、克隆、检查都靠它。
3. **两个隐藏目录（点开头）**：`.drl_param/`（参数库）和 `.drl_src_code/`（源码模板库）。它们才是真正的「资产」。

为什么要用点开头的隐藏目录？这是一种约定：`.drl_param` 和 `.drl_src_code` 是**只读的模板/素材**，平时不该被改动；用户真正操作的设计目录（如 `filt_cicd/`）会被生成在它们之外，避免把生成产物和模板混在一起。

#### 4.1.2 核心流程

顶层目录与构建流程的关系，可以用下面这张「数据流」来理解：

```
.drl_param/<模块>_1.param   ──┐  (用户编辑参数)
                              │
.drl_src_code/<模块>/        ──┤  (只读模板：rtl/ octave/ sim/ log/)
                              │
            dsp_rtl_lib.sh -d ─┴──►  <模块>/   ← 生成到工作目录的设计目录
                                      ├ rtl/      (参数化后的 RTL)
                                      ├ octave/   (参数化后的 GRM)
                                      ├ sim/      (TB + 生成的 stimuli/response)
                                      ├ vvp/      (编译产物，构建后才出现)
                                      ├ vcd/      (波形，构建后才出现)
                                      └ log/      (仿真日志)
```

关键直觉：**`.drl_src_code` 是「模具」，`<模块>/` 是「成品」**。`dsp_rtl_lib.sh` 是把模具压成成品的机器，而 `.drl_param` 里的参数就是调节模具的旋钮。

#### 4.1.3 源码精读

脚本里有一个 `FOLDERS` 数组，把 8 个支持的模块名硬编码了下来，这其实就是「顶层源码模板库的清单」：

[dsp_rtl_lib.sh:249-258](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L249-L258) —— `FOLDERS` 数组列出全部 8 个模块（filt_cicd / filt_cici / filt_fir / filt_mac / filt_ppd / filt_ppi / sgen_cordic / sgen_nco），与 `.drl_src_code/` 下实际存在的 8 个子目录一一对应。

而在 `dsp_rtl_lib.sh` 的 `-help` 文本里，也把合法的 `<module_name>` 列了一遍，并规定了参数文件命名格式 `file_name = <module_name>_<instance_number>.param`：

[dsp_rtl_lib.sh:55-74](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L55-L74) —— 说明 `.param` 文件的命名约定 `<模块名>_<实例号>.param`，实例号表示「要从同一模块生成多少份实例，从 1 开始」。

`.drl_param/` 目录里正好躺着这 8 份模板参数文件，例如：

[.drl_param/filt_cicd_1.param](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_param/filt_cicd_1.param) —— CIC 抽取滤波器的默认参数（抽取因子 4、级数 5、差分延迟 1、相位 0、输入位宽 8）。注意它是「键 = 值」三列格式，脚本后续会用 `awk` 拆出第 1 列（参数名）和第 3 列（数值）。

#### 4.1.4 代码实践

**实践目标**：亲手核对「顶层三件套」与「两个隐藏目录」的存在。

**操作步骤**：

1. 在仓库根目录执行 `ls -la`（`-a` 才能看到点开头的隐藏目录）。
2. 执行 `ls .drl_param/` 与 `ls .drl_src_code/`。

**需要观察的现象**：
- `ls -la` 应能看到 `README.md`、`LICENSE`、`dsp_rtl_lib.pdf`、`dsp_rtl_lib.sh` 四个文件，以及 `.drl_param`、`.drl_src_code`、`.gitignore` 三个隐藏项。
- `.drl_param/` 里有 8 个 `.param` 文件；`.drl_src_code/` 里有 8 个模块子目录。

**预期结果**：8 份参数文件与 8 个源码模块目录一一对应（filt_cicd / filt_cici / filt_fir / filt_mac / filt_ppd / filt_ppi / sgen_cordic / sgen_nco）。这是 DRL 当前全部 Stable 模块，与 README 的模块清单一致。

#### 4.1.5 小练习与答案

**练习 1**：README 把 DC 和 IIR 标为 Planned（计划中）。为什么 `.drl_src_code/` 和 `.drl_param/` 里都没有它们？

> **答案**：Planned 表示尚未实现。`.drl_src_code/` 只收录已 Stable 的模块源码模板，`.drl_param/` 也只为已实现模块提供参数模板。等 DC/IIR 实现完成后，才会在两个目录里各新增一份。

**练习 2**：`.drl_param/filt_cicd_1.param` 文件名里的 `_1` 是什么含义？

> **答案**：是 `<instance_number>`（实例号），表示这是该模块的第 1 个实例配置。命名规则 `<模块名>_<实例号>.param` 允许为同一模块准备多组不同参数。脚本注释里也注明该实例号选项「尚未完全支持」。

---

### 4.2 模块标准子目录结构

#### 4.2.1 概念说明

DRL 的核心工程美学是**统一**：8 个模块长得一模一样。只要学会看懂一个模块的目录结构，就能看懂全部。README 用 `filt_ppd` 为例画出了这份「标准模板」：

```
filt_ppd
 |_ log          ← 仿真日志（*.log）
 |_ octave       ← 黄金参考模型脚本（*.m）
 |_ rtl          ← 可综合 Verilog 源码（*.v）
 |_ sim
 |   |_ testbench        ← SystemVerilog 测试台（*_tb.sv）
 |   |_ testcases
 |       |_ response     ← Octave 生成的「标准答案」数据（*.dat）
 |       |_ stimuli      ← Octave 生成的激励数据 + 参数头（*.dat、defines_*.sv）
 |_ vcd           ← 波形文件（*.vcd）
 |_ vvp           ← Icarus Verilog 编译产物（*.vvp）
```

注意 `rtl` 与 `octave`、`sim/testbench` 是「输入」，`stimuli` 与 `response`、`log`、`vcd`、`vvp` 是「输出」。输入是手写的源码，输出是机器跑出来的产物。

#### 4.2.2 核心流程

脚本里有第二个数组 `SUB_FOLDERS`，精确列出了源码模板库里**每个模块必须具备**的子目录，并在克隆后做一次「自检」——任何一个缺失就报 `exit 5` 错误退出：

```
克隆仓库 (-git)
   │
   ▼
检查 .drl_src_code/<每个模块>/ 下是否齐全这 6 个子目录：
   rtl / sim/testcases/stimuli / sim/testcases/response
   sim/testbench / octave / log
   │
   ├─ 全部存在  → 打印 "Source library check <模块> PASSED"
   └─ 任一缺失  → 打印 "<模块> FAILED" 并 exit 5
```

注意：自检清单里**没有** `vvp` 和 `vcd`。这印证了一个重要事实——`vvp/`（编译产物）和 `vcd/`（波形）是**构建时才生成**的目录，源码模板库里不预置它们。

#### 4.2.3 源码精读

README 里的标准目录树定义：

[README.md:67-81](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L67-L81) —— `Folder Structure` 一节，以 `filt_ppd` 为例给出每个模块的标准目录树，包含 `log / octave / rtl / sim/{testbench,testcases/{response,stimuli}} / vcd / vvp`。

脚本里与之对应的「校验清单」：

[dsp_rtl_lib.sh:261-268](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L261-L268) —— `SUB_FOLDERS` 数组定义每个模块必须存在的 6 个子目录（注意：刻意排除了 `vvp`、`vcd`，因为它们是构建产物）。

[dsp_rtl_lib.sh:270-286](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L270-L286) —— 双重循环逐模块、逐子目录检查；任一缺失即 `exit 5`（faulty source sub-folder），全部通过则打印 `PASSED`。

我们用 `filt_cicd` 的真实内容来印证（来自 `git ls-files`，即 git 实际跟踪的文件）：

```
filt_cicd/
├ log/empty_file                          ← 占位（目录暂空）
├ octave/
│   ├ CICFilter.m        ← CIC 黄金参考模型
│   ├ gen_defines.m      ← 生成 defines_*.sv（把参数注入 TB）
│   ├ quantize.m         ← 定点量化辅助
│   ├ quantizer.m        ← 定点量化辅助
│   └ stimuli.m          ← 激励/响应生成主脚本
├ rtl/
│   ├ dff.v              ← 寄存器原语
│   ├ filt_cicd.v        ← CIC 抽取滤波器主模块
│   └ shift_register.v   ← 移位寄存器原语
├ sim/testbench/
│   └ filt_cicd_tb.sv    ← SystemVerilog 测试台
└ sim/testcases/
    ├ response/empty_file  ← 占位（构建后填 response_tc_*_mat.dat）
    └ stimuli/empty_file   ← 占位（构建后填 stimuli_tc_*_mat.dat、defines_*.sv）
```

注意 `log/`、`response/`、`stimuli/` 里此刻只有一个 `empty_file`——它们要等 Octave 跑完 `stimuli.m` 才会有真正的 `.dat` 文件。

#### 4.2.4 代码实践

**实践目标**：验证「8 个模块长得一模一样」这条统一性。

**操作步骤**：

1. 执行 `ls .drl_src_code/` 列出全部模块。
2. 对其中任意两个模块（如 `filt_fir`、`sgen_nco`）分别执行 `find .drl_src_code/<模块> -type d`，对比它们的目录骨架。

**需要观察的现象**：两个模块的目录树应完全一致，都包含 `log / octave / rtl / sim / sim/testbench / sim/testcases / sim/testcases/response / sim/testcases/stimuli`。

**预期结果**：目录骨架 100% 相同，只有 `rtl/` 和 `octave/` 里的具体 `.v` / `.m` 文件名因模块而异。这种统一性正是 DRL 能用一份脚本驱动所有模块的基础。

#### 4.2.5 小练习与答案

**练习 1**：`sim/testcases/stimuli/` 和 `sim/testcases/response/` 在源码模板里都只有一个 `empty_file`。这两个目录最终分别由谁、在什么时候填充？

> **答案**：都由 Octave 在设计阶段填充。运行 `octave stimuli.m` 后，`stimuli/` 得到激励数据 `stimuli_tc_*_mat.dat` 与参数头 `defines_*.sv`；`response/` 得到对应的「标准答案」`response_tc_*_mat.dat`（即 GRM 的比特真输出）。`empty_file` 只是为了让 git 跟踪这两个暂空的目录。

**练习 2**：为什么源码模板里**没有** `vvp/` 和 `vcd/` 这两个目录，而 README 的目录树里却列了它们？

> **答案**：README 画的是「设计完成并仿真后」的完整目录；而 `.drl_src_code/` 是只读模板，只预置源码与占位目录。`vvp/`（Icarus Verilog 编译出的 `.vvp`）和 `vcd/`（仿真波形 `.vcd`）是构建产物，由脚本在设计/仿真流程中 `mkdir` 创建，且被 `.gitignore` 排除在版本库之外。

---

### 4.3 源码模板与构建产物

#### 4.3.1 概念说明

这是本讲最容易混淆、也最关键的一点：**同样是 `filt_cicd.v`，在 `.drl_src_code/filt_cicd/rtl/` 里和在生成出来的 `filt_cicd/rtl/` 里，含义不同。**

- **源码模板（template）**：位于 `.drl_src_code/`，里面写的是**默认参数**。它是「带旋钮的模具」，对所有用户都一样，是只读的、git 跟踪的。
- **构建产物（generated design）**：位于工作目录下的 `filt_cicd/`，是脚本根据你的 `.param` 把模板「旋钮拧到指定值」之后复制出来的实例，是可运行、可仿真的成品。它里面的 `filt_cicd.v` 已经被 `sed` 注入了你的参数值。

`.gitignore` 是区分这两者的「裁判」：它把所有构建产物（`*.vcd`、`*.vvp`、`*.log`、`work`、`source`、`layout`、`synthesis`、`empty_file` 等）排除在版本库外，保证仓库里只留下手写的模板。

#### 4.3.2 核心流程

`-d`（design）流程把模板变成成品，分三步：

```
1. 复制模板
   cp -rf .drl_src_code/${dsn_name}/  ${DSN_PATH}
   （把整个模块目录原样复制到工作目录）

2. 注入参数（逐行读 .param）
   对 .param 的每一行 (参数名 = 值)：
       sed -i 注入  octave/stimuli.m   ← 改 GRM 的参数
       sed -i 注入  rtl/${dsn_name}.v   ← 改 RTL 的参数

3. 跑 Octave 生成数据 + 创建仿真目录
   octave stimuli.m            → 产出 stimuli/*.dat、response/*.dat、defines_*.sv
   mkdir vvp vcd               → 创建两个构建专用目录
   循环每个 testcases：iverilog 编译 → vvp 仿真 → 波形 .vcd 移入 vcd/
```

这里有一个对初学者极其有用的对照表，把「目录」与「它在流程中的角色」对齐：

| 目录 | 角色 | 何时被填充 | git 是否跟踪 |
|------|------|-----------|--------------|
| `rtl/` | 模板→成品的 RTL | 复制 + sed 注入后 | ✅ 跟踪 |
| `octave/` | 模板→成品的 GRM | 复制 + sed 注入后 | ✅ 跟踪 |
| `sim/testbench/` | SystemVerilog TB | 复制时（成品与模板基本相同） | ✅ 跟踪 |
| `sim/testcases/stimuli/` | 激励数据 | `octave stimuli.m` 后 | ❌ 仅 `empty_file` 占位 |
| `sim/testcases/response/` | 标准答案 | `octave stimuli.m` 后 | ❌ 仅 `empty_file` 占位 |
| `log/` | 仿真日志 | `vvp` 仿真时 | ❌ 仅 `empty_file` 占位 |
| `vvp/` | 编译产物 | `mkdir vvp` + `iverilog` 后 | ❌ 不跟踪 |
| `vcd/` | 波形 | 仿真后 `mv *.vcd vcd` | ❌ 不跟踪 |

#### 4.3.3 源码精读

设计流程第一步——把模板原样复制成设计目录：

[dsp_rtl_lib.sh:404-422](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L404-L422) —— `-d` 流程：先确认是否覆盖（`exit 9` 保护已有设计），再用 `cp -rf .drl_src_code/${dsn_name}/ ${DSN_PATH}` 把整个模块模板复制到工作目录。

设计流程第二步——逐行读 `.param`，用 `sed` 把参数值注入 RTL 与 GRM：

[dsp_rtl_lib.sh:424-433](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L424-L433) —— `awk` 把 `.param` 每行拆成参数名（第 1 列）和数值（第 3 列），再分别 `sed -i` 替换 `octave/stimuli.m` 与 `rtl/${dsn_name}.v` 中的数字。这一步是「模板→成品」的关键，RTL 与 GRM 必须用**同一组参数**，否则比特真比对就会失败。

设计流程第三步——跑 Octave 生成数据，并创建 `vvp`、`vcd` 两个构建专用目录：

[dsp_rtl_lib.sh:450-471](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L450-L471) —— `mkdir vvp vcd` 创建两个之前不存在的目录；随后对每个 testcase 用软链 `defines.sv` 注入参数、`iverilog` 编译到 `vvp/`、`vvp` 仿真，最后 `mv *.vcd vcd` 把波形归档。

`-dev`（开发新模块）流程则展示了「从零搭骨架」时该建哪些目录——注意它**只建**源码与占位目录，**不建** `vvp/vcd`：

[dsp_rtl_lib.sh:689-728](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L689-L728) —— `-dev` 模式用 `mkdir -p` 依次创建 `rtl`、`sim/testbench`、`sim/testcases/stimuli`、`sim/testcases/response`、`octave`、`log`。没有 `vvp`/`vcd`，再次证明二者是仿真时才需要的产物目录。

最后，`.gitignore` 是区分模板与产物的「白纸黑字」：

[.gitignore](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.gitignore) —— 逐行排除构建产物：`*.vcd`（波形）、`*.vvp`（编译输出）、`*.log`（日志）、`work`/`source`/`layout`/`synthesis`（后端流程目录）、`empty_file`（占位文件本身不被重复跟踪）等。凡是它点名的，都是「本地生成、不入库」的产物。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「模板」与「成品」里同名文件的内容差异。

**操作步骤**（在安装了 `iverilog` 与 `octave` 的环境里；若环境不全则做「源码阅读型」对照）：

1. 用编辑器打开**模板**里的 `gp_order` 取值：
   `grep gp_order .drl_src_code/filt_cicd/rtl/filt_cicd.v`
2. 打开参数文件，确认你的目标值：`grep gp_order .drl_param/filt_cicd_1.param`（默认是 5）。
3. 运行设计流程：`./dsp_rtl_lib.sh -demo`（它内部会对 `filt_cicd_1.param` 走 `-d`）。
4. 设计完成后，查看**成品**里的同名取值：
   `grep gp_order filt_cicd/rtl/filt_cicd.v`

**需要观察的现象**：
- 模板里的 `gp_order` 是模板默认值。
- 成品里的 `gp_order` 已被 `sed` 改成 `.param` 指定的值（5）。
- `filt_cicd/` 下出现了 `vvp/`、`vcd/` 两个模板里没有的目录。

**预期结果**：`filt_cicd/rtl/filt_cicd.v` 与 `.drl_src_code/filt_cicd/rtl/filt_cicd.v` 内容不同（至少 `gp_*` 参数不同）；`filt_cicd/` 多出 `vvp/`、`vcd/`，且其内文件（`.vvp`、`.vcd`）不会出现在 `git status` 里（被 `.gitignore` 排除）。

> **若本地无法运行**（缺 iverilog/octave）：改为「源码阅读型」实践——对照阅读 `dsp_rtl_lib.sh` 第 421、430–431、450 三处，用自己的话写一段说明「模板里的 `gp_order` 是如何一步步变成成品里的最终值的」，并预测 `vvp/`、`vcd/` 是否会被 `git add` 收录（预测：不会）。这种情况标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：假设你把模板里 `filt_cicd.v` 的 `gp_order` 直接改成了 7，然后对 `filt_cicd_1.param`（其中 `gp_order = 5`）跑 `-d`。最终成品里 `gp_order` 是几？为什么？

> **答案**：是 5。因为 `cp -rf` 先把模板（含你改的 7）复制成成品，但随后 `sed` 会按 `.param` 的值再次覆盖。`.param` 的优先级高于手改模板——这正是「参数库驱动生成」的意义：模板只提供结构，真正的取值来自 `.param`。（当然，**不应**手改 `.drl_src_code` 模板，那是只读素材。）

**练习 2**：为什么 `vvp/filt_cicd_1.vvp` 和 `vcd/filt_cicd_1.vcd` 不会被生成，却从不出现在 `git status` 的待提交列表里？

> **答案**：因为 `.gitignore` 用 `*.vvp`、`*.vcd` 把它们排除了。这些是构建产物，因人、因机器、因参数而异，没有入库价值；入库的只有手写模板与占位结构。

---

## 5. 综合实践

把本讲三个模块串起来，完成一项「画出 filt_cicd 完整目录树并分类」的任务。

**任务**：对照 README 的 `Folder Structure` 章节，画出 `filt_cicd` 模块**从模板到仿真完成**的完整目录树，并在每一项后标注它的「身份」：

- 🅣 **模板（template）**：`.drl_src_code` 里手写、git 跟踪的源码。
- 🅖 **生成（generated）**：由 `.param` 驱动 `sed` 注入参数后的成品。
- 🅞 **产物（output）**：Octave / iverilog / vvp 跑出来的数据，被 `.gitignore` 排除。

**建议步骤**：

1. 执行 `find .drl_src_code/filt_cicd -type f`，画出**模板阶段**的树（哪些是 🅣，哪些是 `empty_file` 占位）。
2. 在脑中（或本地跑 `-demo` 后）补上**设计阶段**新增的内容：`rtl/filt_cicd.v`、`octave/stimuli.m` 变成 🅖（已注入参数）；`sim/testcases/stimuli/` 与 `response/` 被 🅞 数据填充。
3. 补上**仿真阶段**新增的目录：`vvp/`、`vcd/`（🅞），以及 `log/` 里的 `*.log`（🅞）。
4. 最终用一张表格总结：8 类路径 × {模板/生成/产物} × {git 是否跟踪}。

**自我检查**：你的分类应满足三条不变量——(a) `rtl/*.v` 与 `octave/*.m` 是 🅣 或 🅖（源码），(b) `vvp/`、`vcd/`、`*.dat`、`*.log` 全是 🅞 且 git 不跟踪，(c) `empty_file` 只在产物目录尚未填充时作为占位出现。若三条都成立，说明你已经真正理解了 DRL 的目录契约。

> **若本地无法运行 `-demo`**：则把步骤 2、3 改为「依据 `dsp_rtl_lib.sh` 第 421、430–431、444、450、471 行推理得出」，并在产物项标注「待本地验证」。

## 6. 本讲小结

- DRL 顶层分三类：文档入口（`README.md`/`LICENSE`/`dsp_rtl_lib.pdf`）、构建脚本（`dsp_rtl_lib.sh`）、两个隐藏的只读库（`.drl_param` 参数库、`.drl_src_code` 源码模板库）。
- 全库 8 个模块**目录结构完全统一**：`log / octave / rtl / sim/{testbench, testcases/{response, stimuli}}`，外加构建时才出现的 `vvp / vcd`；脚本用 `FOLDERS` + `SUB_FOLDERS` 两个数组在克隆后做完整性自检（`exit 5`）。
- **源码模板（`.drl_src_code`，只读、git 跟踪）** 与 **构建产物（工作目录下的 `<模块>/`，git 不跟踪）** 是两回事；`-d` 流程用 `cp` 复制 + `sed` 注入参数把前者变成后者。
- `.param` 文件是「键 = 值」三列格式，命名 `<模块>_<实例号>.param`，优先级高于模板默认值。
- `empty_file` 是占位文件，作用是让 git 能跟踪那些暂空、待构建填充的目录（`log`、`stimuli`、`response`）。
- `.gitignore` 是区分模板与产物的裁判：`*.vcd`、`*.vvp`、`*.log` 等一律不入库。

## 7. 下一步学习建议

现在你已经能「看懂仓库骨架」了，下一步自然是「让它动起来」。建议进入 [u1-l3 工具链与构建运行流程](u1-l3-toolchain-and-build-flow.md)，那里会逐参数精读 `dsp_rtl_lib.sh` 的命令行（`-chk`/`-git`/`-d`/`-s`/`-demo`/`-dev`）、`.param` 的 `sed` 注入细节，以及 `design → octave → iverilog → 比对` 的完整回归仿真链路。

如果你对编码风格更感兴趣，也可以先跳到 [u1-l4 统一编码风格与接口约定](u1-l4-coding-style-and-interface.md)，那里以 `dff.v` 为范本讲解全库统一的时序约定与命名前缀。但建议按 u1-l3 → u1-l4 的顺序，先会「跑」再会「读」。
