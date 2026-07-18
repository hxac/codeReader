# abc9 与 liberty：逻辑优化与标准单元映射

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `abc9` 这条 pass 在做什么：它如何把设计里的组合逻辑「切片」成 AIG，交给外部工具 ABC 做布尔网络优化与（LUT）映射，再把结果「缝合」回 RTLIL。
- 区分 `abc9`（面向 FPGA，做 LUT 映射）与旧版 `abc`（面向 ASIC，做标准单元 / liberty 映射），不再混淆 `-liberty` 与 `-lut`。
- 读懂 `dfflibmap` 如何把 Yosys 内部的 `$_DFF_*` 触发器原语映射到 liberty 库中的具体触发器（如 `DFF`、`DFFSR`），并能解释它为什么可能「顺带插入反相器」。
- 理解 `libparse.cc` 如何把手写递归下降解析器 + 自定义缓冲流，把 liberty 文本解析成 `LibertyAst` 树，并用 `LibertyAstCache` 做文件级缓存。

本讲是第 6 单元「核心综合流程」的最后一讲，承接 [u6-l5（techmap 与 simplemap）](u6-l5-techmap-simplemap.md)：techmap 把高层 `$` 单元下沉到门级 `$_` 原语，而本讲负责把这些门级原语「最终落到目标工艺的真实单元上」。

## 2. 前置知识

在进入源码前，先用通俗语言铺几个概念。

**ABC 是什么。** ABC 是加州大学伯克利分校的开源逻辑综合与验证工具（[http://www.eecs.berkeley.edu/~alanmi/abc/](http://www.eecs.berkeley.edu/~alanmi/abc/)）。它擅长两件事：一是把一个布尔网络做「再综合」（化简、重写、平衡），二是把它「工艺映射」(technology mapping) 到某个目标——比如 K 输入的查找表 (LUT)，或某个标准单元库。ABC 是一个**独立的命令行程序**，Yosys 不重写它的算法，而是「调用」它。

**AIG 是什么。** AIG（And-Inverter Graph，与门/反相器图）是一种把任意组合逻辑只用「二输入与门 + 取反边」统一表示的图。一个 `$_AND_` 是一个与门节点，一根带「气泡」的边表示取反。把设计拍扁成 AIG 后，ABC 就能在统一表示上做优化。Yosys 里有 `write_xaiger`（写出带扩展信息的 AIG 文件）和 `read_aiger`（读回）两个命令来做这种交换。

**liberty 是什么。** liberty（`.lib`）是描述工艺单元库的文本格式，由 `library(...) { cell(...) { ... } }` 这种嵌套块组成。每个 `cell` 声明一个真实器件（如一个具体的 D 触发器），里面有 `area`（面积）、若干 `pin`（引脚，带方向 `direction` 和布尔功能 `function`），时序器件还有 `ff(IQ, IQN) { clocked_on: ...; next_state: ...; }` 块描述触发行为。Yosys 综合到最后，需要把内部的抽象 `$_DFF_*`、逻辑门替换成库里这些「有名有姓」的真实单元，后端网表才有意义。

**一个关键区分（本讲最重要的一句话）。** `abc9` 与 `abc` 是两条不同的路：

| 命令 | 目标 | 关键选项 | 典型场景 |
|------|------|----------|----------|
| `abc9` | FPGA | `-lut <宽度>`、`-luts`、`-box` | LUT 映射，**没有 `-liberty`** |
| `abc` | ASIC | `-liberty <file>` | 标准单元映射 |

源码里有铁证：`synth` 命令明确拒绝「不带 `-lut` 的 abc9」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `passes/techmap/abc9.cc` | `abc9` pass 本体：一个 `ScriptPass`，编排「准备→导出 AIG→调 ABC→读回→缝合」的全流程。 |
| `passes/techmap/abc9_exe.cc` | 真正构造 ABC 命令行、运行 `yosys-abc` 二进制、过滤其输出的 pass。 |
| `passes/techmap/abc9_ops.cc` | 一系列辅助子 pass（`-prep_hier`、`-prep_xaiger`、`-prep_lut` 等），由 `abc9.cc` 调用。 |
| `passes/techmap/dfflibmap.cc` | `dfflibmap` pass：解析 liberty，挑选最佳触发器单元，把 `$_DFF_*` 换成库单元。 |
| `passes/techmap/libparse.h` / `libparse.cc` | liberty 解析器：文本 → `LibertyAst` 树，带文件级缓存。 |
| `techlibs/common/abc9_map.v` | abc9 用的 techmap 模板：处理 `-dff` 模式下穿过 ABC 的触发器。 |
| `techlibs/common/synth.cc` | 通用 `synth` 脚本：在 fine 阶段调用 `abc`/`abc9`，并校验「abc9 必须 -lut」。 |
| `examples/cmos/cmos_cells.lib` | 一个极简 liberty 示例库（含 `DFF`、`DFFSR`、`NAND` 等），实践时直接用。 |

## 4. 核心概念与源码讲解

### 4.1 abc9 流程

#### 4.1.1 概念说明

`abc9` 不是「把整个设计丢给 ABC 跑一遍」。它官方 help 里有一段非常关键的提醒：

> Note that this is a logic optimization pass within Yosys that is calling ABC internally. This is not going to "run ABC on your design". It will instead run ABC on logic snippets extracted from your design.

也就是说，`abc9` 的策略是：**把设计按模块切成「逻辑片段」**，对每个片段单独导出成一个 AIG 文件，调用一次 ABC，再把 ABC 优化/映射后的结果读回、替换原来的逻辑。寄存器（触发器）默认不进 ABC，作为边界保留；只有加 `-dff` 才让简单的 `$_DFF_[NP]_` 也穿过 ABC。

还要记住：`abc9` 是为 **FPGA** 设计的，它做的是 **LUT 映射**。它**不接受 `-liberty`**——这是与旧 `abc` 最大的区别。

#### 4.1.2 核心流程

`abc9` 本身是个 `ScriptPass`（参见 [u4-l2](u4-l2-script-pass-synth-prep.md)），自身不做算法，只编排若干阶段标签。其流程可以概括为：

```text
abc9
├── check     : abc9_ops -check              （检查设计是否合法）
├── map       : 准备工作
│   ├── abc9_ops -prep_hier                  （层次准备）
│   ├── scc -specify -set_attr abc9_scc_id   （标记组合环）
│   ├── abc9_ops -prep_bypass                （把不可映射的 box 旁路化）
│   ├── design -stash / -load                （在多个 design 快照间切换）
│   └── techmap -map +/abc9_map.v            （用 abc9_map.v 处理 -dff 的触发器）
├── pre       : abc9_ops -break_scc -prep_delays -prep_xaiger
│             + read_verilog +/abc9_model.v  （建 AIG、断环、加延迟 box）
├── exe       : 对每个选中模块，逐个跑 ABC    ★核心★
│   ├── write_xaiger input.xaig              （RTLIL → AIG 文件）
│   ├── abc9_exe                              （运行 yosys-abc 二进制）
│   ├── read_aiger output.aig                （AIG → RTLIL）
│   └── abc_ops_reintegrate                  （把结果缝合回原设计）
└── unmap     : techmap + abc9_unmap.v，清理临时 box
```

ABC 内部执行什么脚本？`abc9` 在 `on_register()` 时把几套默认脚本预填进 `RTLIL::constpad`，例如默认脚本是：

```
+&scorr; &sweep; &dc2; &dch -f -r; &ps; &if {W} {D} {R} -v; &mfs
```

这里的 `&` 前缀是 ABC 的「新引擎」（`&scorr` 等是 ABC 内部命令），`{W}`/`{D}`/`{R}` 是占位符，分别会被 `-lut` 宽度、`-D` 延迟目标、`-lut` 库文件替换。`&if` 就是 ABC 的 LUT 映射主命令。脚本以 `+` 开头表示「这是一串逗号分隔的内联命令」。

#### 4.1.3 源码精读

**① abc9 是 ScriptPass，预填默认 ABC 脚本。**
[passes/techmap/abc9.cc:36-89](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/abc9.cc#L36-L89) — `Abc9Pass` 继承自 `ScriptPass`；`on_register()` 把 `abc9.script.default`、`...default.area`、`...flow` 等几套 ABC 脚本写进 `RTLIL::constpad`（一种只在启动时写、之后只读的全局字符串表）。

**② help 明确「不是在整个设计上跑 ABC」「面向 FPGA」。**
[passes/techmap/abc9.cc:96-98](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/abc9.cc#L96-L98) — help 文本写道该 pass 用于「technology mapping of the current design to a target FPGA architecture」（面向 FPGA）。
[passes/techmap/abc9.cc:165-170](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/abc9.cc#L165-L170) — 提醒「这是 Yosys 内部调用 ABC 的逻辑优化 pass，不是在你的整个设计上跑 ABC」。这段话直接解释了 4.1.1 的设计哲学。

**③ help 的选项里根本没有 `-liberty`，只有 `-lut`/`-luts`/`-box`。**
[passes/techmap/abc9.cc:129-164](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/abc9.cc#L129-L164) — 选项是 `-lut <width>`、`-luts`、`-maxlut`、`-box <file>`、`-dff`、`-script`、`-D` 等，全部围绕 LUT/FPGA。`synth` 里也强制 `abc9` 必须配 `-lut`：
[techlibs/common/synth.cc:247-248](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L247-L248) — `if (abc == "abc9" && !lut) log_cmd_error("ABC9 flow only supported for FPGA synthesis (using '-lut' option)")`。

**④ `execute()` 解析参数后调用 `run_script()`。**
[passes/techmap/abc9.cc:195-268](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/abc9.cc#L195-L268) — 注意 `dff_mode`、`lut_mode`、`box_file` 等开关会被拼进 `exe_cmd`（最终传给 `abc9_exe`），并先从 `scratchpad` 读取默认值，再被命令行参数覆盖。这呼应了 u4 讲过的 scratchpad 传参机制。

**⑤ `script()` 的 `check` / `map` 阶段——准备设计快照。**
[passes/techmap/abc9.cc:279-342](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/abc9.cc#L279-L342) — 这一段用 `design -stash`/`-load` 在 `$abc9`、`$abc9_map` 等命名快照之间来回切换，把「旁路 box」和（`-dff` 时的）触发器整理成 ABC 能理解的形态，最后用 `techmap -wb -max_iter 1 -map %$abc9_map -map +/abc9_map.v` 应用 `abc9_map.v` 模板。`check_label("map")` 是 ScriptPass 划分阶段的固定写法（见 u4-l2）。

**⑥ `abc9_map.v` 模板——只在 `-dff` 时起作用。**
[techlibs/common/abc9_map.v:1-27](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/abc9_map.v#L1-L27) — 整个文件包在 `` `ifdef DFF `` 里（只有 `-dff` 时 `DFF` 宏才被定义）。它通过 `(* techmap_celltype = "$_DFF_[PN]_" *)` 声明自己匹配两种边沿触发器，并依 `_TECHMAP_WIREINIT_Q_`（初值）分支：当初值为 0 时插入一个 `$__DFF_x__$abc9_flop` 辅助 box（让 ABC 知道这个 flop 的存在），否则给触发器打上 `(* abc9_keep *)` 属性让它原样保留。这示范了 techmap 的「模板替换 + 属性驱动」套路（参见 u6-l5）。

**⑦ `pre` 阶段——把 RTLIL 翻译成 AIG。**
[passes/techmap/abc9.cc:344-370](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/abc9.cc#L344-L370) — 先 `read_verilog -icells -lib -specify +/abc9_model.v` 把 ABC box 的仿真模型注册成库，再 `abc9_ops -break_scc -prep_delays -prep_xaiger` 断开组合环、为穿 box 的路径加延迟、生成 AIG。`-prep_lut`/`-prep_box` 在没给 `-lut`/`-box` 时自动推导。

**⑧ `exe` 阶段——逐模块调 ABC（abc9 的心脏）。**
[passes/techmap/abc9.cc:372-449](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/abc9.cc#L372-L449) — `aigmap` 先把门转成 AIG；然后**对每个选中模块单独**做：建临时目录、`write_xaiger` 导出 AIG、`abc9_exe` 跑 ABC、`read_aiger` 读回、`abc_ops_reintegrate` 缝合。核心片段如下：

```cpp
// passes/techmap/abc9.cc:412-433
run_nocheck(stringf("write_xaiger -map %s/input.sym %s %s/input.xaig",
                    tempdir_name, dff_mode ? "-dff" : "", tempdir_name));
int num_outputs = active_design->scratchpad_get_int("write_xaiger.num_outputs");
...
if (num_outputs) {
    std::string abc9_exe_cmd;
    abc9_exe_cmd += stringf("%s -cwd %s", exe_cmd.str(), tempdir_name);
    if (!lut_mode)  abc9_exe_cmd += stringf(" -lut %s/input.lut", tempdir_name);
    if (box_file.empty()) abc9_exe_cmd += stringf(" -box %s/input.box", tempdir_name);
    else                  abc9_exe_cmd += stringf(" -box %s", box_file);
    run_nocheck(abc9_exe_cmd);
    run_nocheck(stringf("read_aiger -xaiger -module_name %s$abc9 %s/output.aig", mod, tempdir_name));
    run_nocheck(stringf("abc_ops_reintegrate -map %s/input.sym %s", tempdir_name, dff_mode ? "-dff" : ""));
} else
    log("Don't call ABC as there is nothing to map.\n");
```

注意「输出数为 0 就不调 ABC」这个小优化——纯时序、无组合输出的模块会被跳过。

**⑨ `abc9_exe.cc` 真正运行 `yosys-abc` 二进制。**
[passes/techmap/abc9_exe.cc:286-291](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/abc9_exe.cc#L286-L291) — 构造命令行 `"<yosys-bindir>/yosys-abc" -s -f <tempdir>/abc.script 2>&1`，通过 `run_command(...)` 执行，并用 `abc9_output_filter` 过滤/改写 ABC 的输出使其更易读。这就是「Yosys 把 ABC 当外部进程调用」的物理证据。

#### 4.1.4 代码实践

> **目标**：亲手跑一次 abc9，验证它做的是 **LUT 映射**，并确认它**没有** `-liberty` 选项。

操作步骤（假设你已按 [u1-l2](u1-l2-build-and-run.md) 构建出 `./build/yosys`）：

1. 进入交互 shell：`./build/yosys`
2. 依次执行：
   ```
   read_verilog examples/cmos/counter.v
   hierarchy -top counter
   proc; opt; techmap; opt            # 走到门级 $_ 原语
   stat                               # 先看一眼优化前的单元
   abc9 -lut 4                        # 用 4 输入 LUT 做 FPGA 映射
   stat                               # 再看映射后多了哪些 $lut / LUT 单元
   ```
3. 退出后，再故意试一次 `abc9 -liberty examples/cmos/cmos_cells.lib`，观察报错。

**需要观察的现象：**
- `abc9 -lut 4` 之后，`stat` 里会出现若干 `$lut` 单元（abc9 把组合逻辑打包成查找表）。
- `abc9 -liberty ...` 会被 Yosys 拒绝（未知选项报错），这印证了「abc9 不做 liberty 映射」。

**预期结果：** LUT 映射成功产生 `$lut`；`-liberty` 不可用。（具体 `$lut` 数量取决于 counter 的组合逻辑规模，待本地验证。）

> 本实践修正了任务书中「`abc9 -liberty`」的写法——该选项在 abc9 中并不存在，标准单元映射请用下一节的 `abc -liberty`。这是基于源码事实的更正，而非臆测。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `abc9` 要「逐模块」调用 ABC，而不是一次性把整个设计丢进去？
<details><summary>参考答案</summary>因为 ABC 主要处理组合逻辑，而设计的边界（触发器、不同时钟域）天然把逻辑切成片；逐模块切片能让每段都成为规模可控、边界清晰的组合网络，便于 ABC 优化与映射，也方便事后用 `abc_ops_reintegrate` 精确缝合回原位置。</details>

**练习 2**：`abc9 -script +a;b;c` 里的前导 `+` 是什么意思？
<details><summary>参考答案</summary>`+` 表示「把后续字符串当作内联的 ABC 命令串」，其中的逗号会被替换为空格再传给 ABC（见 help 对 `-script` 的说明 [abc9.cc:113-120](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/abc9.cc#L113-L120)）。</details>

### 4.2 dfflibmap

#### 4.2.1 概念说明

经过前面几讲，设计里的触发器已经统一成了 Yosys 的内部原语 `$_DFF_P_`（上升沿）、`$_DFF_N_`（下降沿），以及带使能/复位的各种变体 `$_DFFE_*`、`$_DFFSR_*` 等。但这些原语是「抽象的」，真实芯片里并没有叫 `$_DFF_P_` 的器件——真实器件是 liberty 库里那些有具体名字（如 `DFF`、`FD1`）、有面积、有时序的单元。

`dfflibmap` 的职责就是：**读一个 liberty 文件，为每种 `$_DFF_*` 原语在库里挑一个「最合适」的真实触发器，然后把设计里的原语替换成它。** 它只管触发器（时序元件），不管组合逻辑门——组合门的 liberty 映射是 `abc`/`abc9` 的事。

「最合适」的标准是：极性匹配（时钟/置位/清零的极性要对得上）、引脚最少、面积最小、优先非反相输出。如果库里某个触发器的输入/输出极性与原语相反，`dfflibmap` 会**自动插入反相器**（`$_NOT_`）来凑齐——这正是 help 里那句「This pass may add inverters as needed」的由来。

#### 4.2.2 核心流程

```text
dfflibmap -liberty <file>
│
├── 1. 解析 liberty → LibertyAst 树，合并多文件 → merged.cells   （见 4.3）
│
├── 2. 对「每一种 $_DFF_* 原语」调用 find_cell / find_cell_sr：
│        枚举库里每个 cell，跳过 dont_use，
│        校验 ff 块的 clocked_on/next_state/preset/clear 极性，
│        给引脚打角色标签 C/D/R/S/E/Q/q,
│        按 (引脚数, 面积, 是否非反相) 选最优 → 写入 cell_mappings
│
├── 3. （默认）调用 dfflegalize，把设计里更「宽泛」的 $_DFF* 归一成
│        cell_mappings 能覆盖的那几种原子类型
│
└── 4. 对每个模块执行 dfflibmap()：
        遍历 $_DFF_* 单元 → remove → addCell(库单元名) → 按角色重接端口，
        需要时插入 $_NOT_ （用 notmap 复用已有反相器避免重复）
```

引脚「角色标签」用的是单字符编码：`C`=时钟、`D`=数据、`R`=复位、`S`=置位、`E`=使能、`Q`=同相输出、`q`=反相输出；小写字母（如 `d`）表示该引脚需要取反接入，`'0'`/`'1'` 表示接常数。

#### 4.2.3 源码精读

**① 全局映射表 `cell_mappings`。**
[passes/techmap/dfflibmap.cc:30-34](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L30-L34) — `cell_mapping` 存「目标单元名 + 引脚角色表」，`cell_mappings` 是「原语类型 → cell_mapping」的全局 dict，是步骤 2 的产物、步骤 4 的输入。`execute` 结束时会被 `clear()` 清空（[dfflibmap.cc:712](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L712)）。

**② dfflibmap 支持的全部 `$_DFF_*` 类型清单。**
[passes/techmap/dfflibmap.cc:56-83](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L56-L83) — `logmap_all()` 列出了它会尝试映射的所有原语：`$_DFF_[NP]_`（普通 DFF）、`$_DFF_[NP][NP][01]_`（异步复位 DFF，三位分别编码时钟极性、复位极性、复位值）、`$_DFFE_[NP][NP]_`（带使能）、`$_DFFSR_[NP][NP][NP]_`（带置位/复位）。这串名字本身就是一份「Yosys 时序原语百科」。

**③ `find_cell` 的挑选算法签名。**
[passes/techmap/dfflibmap.cc:237-273](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L237-L273) — 函数签名 `find_cell(cells, cell_type, clkpol, has_reset, rstpol, rstval, has_enable, enapol, dont_use_cells)`。每个布尔参数描述目标原语想要的极性/特性；函数体遍历库单元，先跳过 `dont_use`（[L247-261](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L247-L261)），再用 `parse_pin`/`parse_next_state` 校验 `ff` 块里的 `clocked_on`/`next_state` 极性是否匹配（[L263-273](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L263-L273)）。不匹配就 `continue` 换下一个。

**④ 「引脚最少 + 面积最小 + 优先非反相」的取舍。**
[passes/techmap/dfflibmap.cc:343-353](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L343-L353) — 选中条件：必须有输出；若已有候选，则新单元引脚数更少才胜出（`num_pins > best_cell_pins` 直接淘汰），引脚数相同时面积更小才胜出，并优先保留非反相输出。这是典型的「字典序多目标择优」。

**⑤ `execute`：解析 liberty、逐类型选单元、再映射。**
[passes/techmap/dfflibmap.cc:603-713](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L603-L713) — 关键三段：
- [L656-662](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L656-L662)：对每个 `-liberty` 文件用 `LibertyParser` 解析，`merged.merge(p)` 收集所有 `cell`。
- [L664-688](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L664-L688)：对 4.2.2 列举的每种原语调一次 `find_cell`/`find_cell_sr`，参数精确编码该原语想要的极性（如 `$_DFF_P_` 是 `clkpol=true`，无复位/使能）。
- [L693-709](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L693-L709)：默认（非 `-prepare`/`-info`）先用 `dfflegalize` 把设计归一，再对每个非黑盒模块跑 `dfflibmap(design, module)` 做实际替换。

**⑥ 真正的「换单元」逻辑，含自动插反相器。**
[passes/techmap/dfflibmap.cc:497-569](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L497-L569) — 先建 `SigMap sigmap` 与 `notmap`（`SigBit → 现有 $_NOT_ 集合`，用于复用反相器，[L501-510](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L501-L510)）。对每个待映射单元：`remove` 旧单元 → `addCell` 新库单元 → 按角色标签重接端口。其中反相处理（[L533-560](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L533-L560)）是精髓：大写角色（`'A'..'Z'`）直连；小写 `'a'..'z'` 表示该引脚要取反接入，于是 `module->NotGate(...)` 插一个反相器；`'q'`（反相输出）则尽量复用 `notmap` 里已有的反相器（`has_q && has_qn` 时改接 `$_NOT_` 的 Y，避免重复插入）。

#### 4.2.4 代码实践

> **目标**：用 `examples/cmos/cmos_cells.lib` 把 counter 的触发器映射成库里的 `DFF`。

操作步骤：

1. 查看 `examples/cmos/cmos_cells.lib`，确认库里有 `cell(DFF)`，其 `ff(IQ, IQN) { clocked_on: C; next_state: D; }`，引脚 `C`(clock)、`D`(input)、`Q`(output, function "IQ")。
2. 在 `yosys-tutorial/` 同级运行：
   ```
   ./build/yosys -p "
     read_verilog examples/cmos/counter.v;
     read_verilog -lib examples/cmos/cmos_cells.v;
     synth;
     dfflibmap -liberty examples/cmos/cmos_cells.lib;
     stat;
   "
   ```
3. 再单独跑一次带 `-info` 的版本观察「它打算映射到什么」：
   ```
   ./build/yosys -p "
     read_verilog examples/cmos/counter.v;
     read_verilog -lib examples/cmos/cmos_cells.v;
     synth;
     dfflibmap -info -liberty examples/cmos/cmos_cells.lib;
   "
   ```

**需要观察的现象：**
- `dfflibmap` 日志会打印类似 `cell DFF (noninv, pins=3, area=18.00) is a direct match for cell type $_DFF_P_.`，说明它选中了 `DFF`。
- `stat` 中 `$_DFF_P_` 数量下降、`DFF` 数量上升。
- `-info` 模式还会打印一条 `dfflegalize` 命令行，但不真正修改设计。

**预期结果：** counter 的 `$_DFF_P_` 被替换为库单元 `DFF`（cmos_cells.lib 恰好定义了这个 DFF，极性匹配）。若库里没有匹配单元，对应原语会显示为 `unmapped dff cell`。（具体计数待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：`dfflibmap` 与 `abc -liberty` 各自负责映射哪类单元？
<details><summary>参考答案</summary><code>dfflibmap</code> 只映射时序元件（<code>$_DFF_*</code>/<code>$_DFFE_*</code>/<code>$_DFFSR_*</code>）；<code>abc -liberty</code> 映射组合逻辑门。两者都读 liberty，但作用于不同的内部单元集合。help 也建议「先 dfflibmap（可能加反相器），再映射组合路径」。</details>

**练习 2**：为什么 `dfflibmap` 内部要建 `notmap`（反相器映射表）？
<details><summary>参考答案</summary>当目标库触发器需要反相输入/输出时，<code>dfflibmap</code> 要插入 <code>$_NOT_</code>。用 <code>notmap</code> 记录每个信号位上已有的反相器，可以复用而非重复插入，减少面积。见 [dfflibmap.cc:501-510](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L501-L510) 与 [L533-548](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L533-L548)。</details>

**练习 3**：`-prepare` 和 `-map-only` 模式分别跳过了哪一步？
<details><summary>参考答案</summary><code>-prepare</code> 只做步骤 2（选单元 + dfflegalize 把原语归一成匹配类型），不做步骤 4 的实际替换；<code>-map-only</code> 跳过 dfflegalize，只替换已经「恰好类型正确」的单元；<code>-info</code> 只打印信息不改设计（[dfflibmap.cc:693-709](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L693-L709)）。</details>

### 4.3 libparse liberty

#### 4.3.1 概念说明

`dfflibmap`（以及 `abc -liberty`、`read_liberty`）都需要先把 liberty **文本**变成程序能查的结构。这件事由 `passes/techmap/libparse.cc` + `libparse.h` 完成。它的产物是一棵 `LibertyAst` 树：

- `LibertyAst::id` 是关键字（如 `"library"`/`"cell"`/`"pin"`/`"area"`/`"ff"`）；
- `LibertyAst::value` 是 `key: value` 冒号后的值（如 `area: 6;` 里的 `"6"`）；
- `LibertyAst::args` 是圆括号里的参数（如 `cell(DFF)` 的 `"DFF"`、`ff(IQ, IQN)` 的 `"IQ"`/`"IQN"`）；
- `LibertyAst::children` 是花括号里的子节点。

例如 `cmos_cells.lib` 里的

```text
cell(DFF) {
  area: 18;
  ff(IQ, IQN) { clocked_on: C; next_state: D; }
  pin(C) { direction: input; clock: true; }
  pin(Q) { direction: output; function: "IQ"; }
}
```

会被解析成一个 `id="cell"`、`args=["DFF"]` 的节点，其 `children` 包含 `area`(value="18")、`ff`(args=["IQ","IQN"], children=[clocked_on, next_state]) 和两个 `pin` 节点。`dfflibmap` 的 `find_cell` 正是靠 `cell->find("ff")`、`ff->find("clocked_on")` 这样的查找在这棵树上取信息。

解析器还做了两件「工程化」的事：一是自带缓冲的输入流 `LibertyInputStream`（支持 `unget`/`peek`），二是 `LibertyAstCache`——同一个 liberty 文件在一次 Yosys 运行里只解析一次，之后命中缓存。

#### 4.3.2 核心流程

liberty 文法非常简单，本质是三类结构反复嵌套：

```text
语句   := 标识符 [ ( 参数列表 ) ] [ : 值 ; | { 语句... } ]
参数   := 标识符 (',' 标识符)*
```

`LibertyParser::parse()` 是手写递归下降：读一个 token，若是标识符 `v`，就新建 `LibertyAst` 并把 `id` 设为它；随后循环读 token——遇到 `(` 收集 `args`、遇到 `:` 读 `value`、遇到 `{` 递归解析 `children`、遇到 `;` 或换行结束本语句。词法器 `lexer()` 把字符流切成三种 token：`v`（标识符/字符串/`[...]` 范围）、`n`（换行）、其他单字符（`(` `)` `{` `}` `:` `;` `,`）。

缓冲输入流的设计要点：为了支持「回退一个字符」(`unget`) 又不依赖 `istream::unget` 的可靠性，`LibertyInputStream` 在内部 `vector<unsigned char> buffer` 上维护 `[buf_pos, buf_end)` 窗口，读取时先把磁盘按 4KB 块读进缓冲区，再从缓冲区取字符，从而把「慢的逐字符磁盘 IO」摊销成「快的内存访问」。

#### 4.3.3 源码精读

**① `LibertyAst` 数据结构。**
[passes/techmap/libparse.h:36-46](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.h#L36-L46) — `id`/`value`/`args`/`children` 四件套 + `find(name)` 在 children 里按 id 查找。这就是 4.3.1 描述的那棵树的节点。

**② liberty 布尔表达式（用于解析 `function`）。**
[passes/techmap/libparse.h:48-101](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.h#L48-L101) — `LibertyExpression` 把 `"IQ"`、`"(A*B)'"` 这样的功能串解析成 AND/OR/NOT/XOR/PIN 树，供 `dfflibmap` 的 `parse_next_state` 判断「next_state 是不是带使能的表达式」时复用（见 [dfflibmap.cc:118-119](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L118-L119)）。

**③ 自带缓冲的输入流。**
[passes/techmap/libparse.h:103-143](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.h#L103-L143) — `get()`/`peek()`/`consume()`/`unget()` 都在 `buffer` 上做指针运算；只有缓冲耗尽时才调 `get_cold()`/`peek_cold()` 触发真正的磁盘读取（[libparse.cc:75-118](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.cc#L75-L118)）。`extend_buffer_once()` 每次读 4KB，并通过 `memmove` 保留最后一个字符以支持 `unget`。

**④ 文件级缓存 `LibertyAstCache`。**
[passes/techmap/libparse.h:146-160](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.h#L146-L160) 与 [libparse.cc:50-71](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.cc#L50-L71) — `cached_ast(fname)` 命中则返回已解析的树，`parsed_ast(fname, ast)` 注册新解析结果。带文件名的 `LibertyParser` 构造函数会先查缓存：
[passes/techmap/libparse.h:203-213](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.h#L203-L213) — 命中 `cached_ast` 就直接用，否则 `parse(true)` 后 `parsed_ast` 存入缓存。这样 `dfflibmap` 与 `abc` 读同一个 `.lib` 时只解析一次。

**⑤ 递归下降解析主循环。**
[passes/techmap/libparse.cc:631-750](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.cc#L631-L750) — 这是 4.3.2 流程的直接对应物。关键分支：
- [L654-659](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.cc#L654-L659)：token 必须是 `v`（标识符），作为 `ast->id`。
- [L670-704](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.cc#L670-L704)：`:` 分支读 `value`，并兼容「未加引号的表达式串」「`+/-/*/!` 前缀」等 liberty 野生写法（`consume_wrecked_str`）。
- [L706-728](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.cc#L706-L728)：`(` 分支收集 `args`。
- [L730-744](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.cc#L730-L744)：`{` 分支递归 `parse(false)` 收集 `children`，遇到 `}`（返回 NULL）结束。

**⑥ 多文件合并 `LibertyMergedCells::merge`。**
[passes/techmap/libparse.h:217-235](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.h#L217-L235) — 校验顶层 `id` 必须是 `"library"`，然后把所有 `cell`（`args.size()==1`）平铺进一个 `cells` 向量，让 `dfflibmap` 无视「来自哪个文件」统一挑选。这支撑了 `dfflibmap -liberty a.lib -liberty b.lib` 的多库合并。

#### 4.3.4 代码实践

> **目标**：通过「源码阅读 + 小实验」理解 liberty 文本如何变成 `LibertyAst`。这是源码阅读型实践。

操作步骤：

1. 打开 `examples/cmos/cmos_cells.lib`，对照 4.3.1 画出 `cell(DFFSR)` 这棵子树（注意它的 `ff("IQ","IQN")` 带了引号、还有 `preset`/`clear`，且文件里有一行 `; // empty statement`）。
2. 在 `libparse.cc:631` 的 `parse()` 设想一次调用：读到 `cell` → `(` → `DFFSR` → `)` → `{` → 递归解析 `area`/`ff`/`pin`… → `}`。
3. 验证「容错」：文件里那句孤立的 `;` 和块尾多余 `;` 能被正确忽略——对应 [libparse.cc:642-643](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.cc#L642-L643) 的 `while ((tok == 'n') || (tok == ';'))` 跳过。
4. 用 `filterlib`（Yosys 自带的小工具，复用同一份 `libparse.cc`，由 `FILTERLIB` 宏编译）把 `cmos_cells.lib` 重写一遍，观察输出结构是否与原文件对应。

**需要观察的现象/预期结果：**
- 你画出的 `cell(DFFSR)` 子树应包含：`area`(18)、`ff`(args=["IQ","IQN"], children: clocked_on=C, next_state=D, preset=S, clear=R)、4 个 `pin`(C/D/Q/S/R 中对应的几个)。
- `filterlib` 重新输出的 liberty 与输入在结构上等价（可能格式化不同）。具体输出格式待本地验证。

> 小提示：`libparse.cc` 顶部 `#ifdef FILTERLIB` 分支（[libparse.cc:30-44](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.cc#L30-L44)）说明这同一份源码既被编进 Yosys 主程序，也被单独编译成独立的 `filterlib` 工具——一份代码两用的典型技巧。

#### 4.3.5 小练习与答案

**练习 1**：`LibertyAst::value` 和 `LibertyAst::args` 分别来自 liberty 文法的哪部分？
<details><summary>参考答案</summary><code>args</code> 来自 <code>name(arg1, arg2, ...)</code> 圆括号里的参数；<code>value</code> 来自 <code>key: value;</code> 冒号后的值。例如 <code>cell(DFF)</code> 有 args=["DFF"] 无 value，<code>area: 18;</code> 有 value="18" 无 args。</details>

**练习 2**：为什么 `LibertyInputStream` 要自己维护缓冲，而不直接用 `std::istream::get()`？
<details><summary>参考答案</summary>
为了高效支持 <code>peek</code>/<code>unget</code> 并减少系统调用：它把磁盘内容按 4KB 块批量读进内存 <code>buffer</code>，热路径 <code>get()</code> 只是数组下标前进（[libparse.h:122-128](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.h#L122-L128)），只有缓冲耗尽才走 cold 路径读盘。</details>

**练习 3**：`LibertyAstCache` 解决了什么问题？
<details><summary>参考答案</summary>一次 Yosys 运行中可能多次读同一个 liberty（如 <code>dfflibmap</code> 和 <code>abc</code> 各读一次）。缓存以文件名为键，命中即复用已解析的 AST，避免重复解析大库文件（[libparse.cc:52-71](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.cc#L52-L71)）。</details>

## 5. 综合实践

把三块知识串起来：跑一遍「真正的标准单元综合」，并对照源码解释每一步。

**任务：** 用 `examples/cmos/cmos_cells.lib` 把 counter 综合到这个工艺库，然后回答三个问题。

脚本（保存为 `yosys-tutorial/run-cmos.ys` 之外的任意临时文件，或直接 `-p`）：

```
read_verilog examples/cmos/counter.v
read_verilog -lib examples/cmos/cmos_cells.v

synth                       # proc/opt/techmap... 到门级 $_ 原语
dfflibmap -liberty examples/cmos/cmos_cells.lib   # 触发器 → DFF
abc -liberty examples/cmos/cmos_cells.lib         # 组合门 → NAND/NOR/NOT/BUF
opt_clean

stat -liberty examples/cmos/cmos_cells.lib
write_verilog synth_cmos.v
```

> 注意这里用的是 **`abc -liberty`**（标准单元映射），而不是 `abc9`。这正是 `examples/cmos/counter.ys` 原本的写法。

**需要你回答：**

1. 在 `stat` 输出里，触发器变成了哪个库单元？组合逻辑变成了哪些库单元？请到 `cmos_cells.lib` 里找到它们的 `function` 定义验证功能一致。
2. 对照 [dfflibmap.cc:664-665](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/dfflibmap.cc#L664-L665) 的两行 `find_cell(...,ID($_DFF_N_),...)` / `find_cell(...,ID($_DFF_P_),...)`：counter 用的是上升沿还是下降沿时钟？`find_cell` 是靠 liberty 里 `ff` 块的哪个字段判断极性的？
3. 如果把 `dfflibmap` 和 `abc` 的顺序对调，可能会出什么问题？（提示：见 `dfflibmap` help 里「可能插入反相器，因此建议先 dfflibmap 再映射组合路径」。）

**预期结果：** 设计中的 `$_DFF_P_` 被映射为 `DFF`，组合逻辑被映射为 `NAND`/`NOR`/`NOT`/`BUF` 等库单元，`write_verilog` 输出的 `synth_cmos.v` 里只剩这些具名单元。这是一个端到端可验证的「门级 → 工艺单元」映射。

## 6. 本讲小结

- `abc9` 是面向 **FPGA** 的逻辑优化 + **LUT 映射** pass：它把设计按模块切成 AIG 片段，逐个调用外部 `yosys-abc` 二进制（`abc9_exe`），再把结果缝合回 RTLIL；它**没有 `-liberty`**，只能用 `-lut`/`-luts`/`-box`，`synth -abc9` 也强制要求 `-lut`。
- `abc9` 本体是个 `ScriptPass`，分 `check/map/pre/exe/unmap` 五个阶段；真正运行 ABC 的是 `abc9_exe`，它构造 `yosys-abc -s -f abc.script` 命令行并通过 `run_command` 执行。
- 标准单元（liberty）映射走的是另一条路：触发器由 `dfflibmap -liberty` 负责，组合门由旧版 `abc -liberty` 负责——`examples/cmos/counter.ys` 正是这条经典 ASIC 流程。
- `dfflibmap` 为每种 `$_DFF_*`/`$_DFFE_*`/`$_DFFSR_*` 原语在库里按「极性匹配 + 引脚最少 + 面积最小 + 优先非反相」挑出最佳单元，替换时按角色标签重接端口，必要时自动插入并复用反相器（`notmap`）。
- liberty 文本由 `libparse.cc` 的手写递归下降解析器 + 自带缓冲流 `LibertyInputStream` 解析成 `LibertyAst` 树（`id`/`value`/`args`/`children`），并由 `LibertyAstCache` 做文件级缓存、`LibertyMergedCells` 支持多文件合并。
- 不要把 abc9 当 abc 用：要 LUT 用 abc9，要标准单元用 dfflibmap + abc -liberty。

## 7. 下一步学习建议

- **向后（后端）：** 映射完成后，设计最终要被写出来。下一单元 [u7-l1（write_verilog / write_rtlil / write_json）](u7-l1-write-verilog-rtlil-json.md) 讲后端如何遍历这棵已经满是库单元的 RTLIL 并输出网表。
- **向深（高级内部机制）：** abc9 重度依赖 AIG。若想理解「门级网表如何变成 AIG、AIG 如何用于 SAT/形式验证」，读 [u10-l2（functional IR 与 AIG 表示）](u10-l2-functional-ir-aig.md) 和 [u10-l1（SAT 与形式验证机制）](u10-l1-sat-formal-verification.md)。
- **源码延伸阅读：**
  - `passes/techmap/abc9_ops.cc`：看 `prep_xaiger`/`prep_lut`/`prep_box` 具体如何改写 RTLIL。
  - `passes/techmap/abc.cc`（旧版 abc pass）：对比它与 abc9 在「调用 ABC」方式上的异同，体会 abc9 为何重写。
  - `passes/techmap/dfflegalize.cc`：`dfflibmap` 默认调用的「时序原语归一化」pass。
