# 目标平台综合流程：synth_xilinx / synth_ice40

## 1. 本讲目标

通用 `synth` 命令（见 [u4-l2](u4-l2-script-pass-synth-prep.md)）是「与目标无关」的：它把 Verilog 一路降到 `$_AND_`/`$_NOT_`/`$_DFF_*` 这类单位宽门级原语就停下来，并不关心最终跑在哪块芯片上。但真实的 FPGA/ASIC 物理资源是厂商定义的——iCE40 上的 `SB_LUT4`/`SB_RAM40_4K`/`SB_MAC16`/`SB_DFF`，Xilinx 上的 `LUT6`/`FDRE`/`RAMB36`/`DSP48`。把这些「平台相关知识」缝进综合流程的，正是本讲的两个厂商专属命令 `synth_ice40` 与 `synth_xilinx`。

学完本讲，你应该能够：

- 说清楚「厂商 ScriptPass」相对通用 `synth` 多出了哪些扩展点，以及它们为什么必须按特定顺序排列。
- 对照源码讲出 iCE40 与 Xilinx 两条流程的阶段划分（DSP / BRAM / 算术进位 / FF 合法化 / LUT 映射）。
- 理解多 family 参数化：Xilinx 如何用 `family` 选项在运行时切换 LUT 宽度、DSP/BRAM 模板。
- 读懂 `cells_map.v`、`ff_map.v`、`dsp_map.v` 这类平台映射脚本如何把内部 `$`/`$_` 单元落到厂商原语。
- 亲手综合一个含 BRAM 的设计，定位它调用的 LUT/BRAM 映射子 pass，并解释调用顺序。

## 2. 前置知识

本讲是 advanced 阶段，默认你已掌握下列内容（不讲重复）：

- **ScriptPass 双模式**（[u4-l2](u4-l2-script-pass-synth-prep.md)）：`script()` 里用 `run("...")` 声明要调的子 pass，`check_label("阶段名")` 划分阶段，`help_mode` 下同一份代码只打印命令清单、`run_script` 下才真正执行，二者永远一致。`-run from:to` 用 `block_active` 开闸/关闸来限定执行范围。
- **techmap 是模板替换机**（[u6-l5](u6-l5-techmap-simplemap.md)）：它不懂 RTLIL 语义，只按 cell 类型匹配「模板模块」，把模板内容内联进当前模块并删掉原 cell；模板用 `techmap_celltype`、`_TECHMAP_REPLACE_`、`_TECHMAP_FAIL_`、`_TECHMAP_CONSTMSK_*` 等属性驱动。
- **abc9 与工艺映射分工**（[u6-l6](u6-l6-abc9-liberty.md)）：FPGA 走 abc9（LUT 映射），标准单元走 `dfflibmap`+`abc -liberty`，二者不可混用。
- **memory 流程**（[u6-l4](u6-l4-memory.md)）：`memory -nomap` 保留 `$mem`，`memory_libmap` 把大存储器映射成块 RAM 原语，`memory_map` 把剩下的零散存储器降成 `$dff`+逻辑。
- **techlibs 是数据资源仓库**（[u8-l1](u8-l1-techlibs-structure.md)）：`_sim` 后缀是仿真模型（`-lib -specify` 读），`_map` 后缀是映射模板（`techmap -map` 读）；厂商目录里 `cells_sim.v` 与 `cells_map.v` 成对出现。

补充一个本讲会用到的运行时全局变量：`RTLIL::constpad` 是一个全局 `dict<string,string>`（[kernel/rtlil.h:752](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L752-L752)），用于存放「编译期就固定的默认参数值」，厂商 pass 在 `on_register()` 里往里写每个器件的 abc9 默认 `-W`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [techlibs/ice40/synth_ice40.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc) | `synth_ice40` 命令实现，iCE40 平台 ScriptPass，编排全部子 pass |
| [techlibs/xilinx/synth_xilinx.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc) | `synth_xilinx` 命令实现，Xilinx 平台 ScriptPass，支持多 family 参数化 |
| [techlibs/ice40/cells_map.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/cells_map.v) | iCE40 终极映射模板，把 `$lut` 落成 `SB_LUT4` |
| [techlibs/xilinx/cells_map.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/cells_map.v) | Xilinx 终极映射模板，含 `__XILINX_SHREG_`（移位寄存器）、`__XILINX_MUXF78`（硬 mux）、`__XILINX_SHIFTX` |
| [techlibs/ice40/ff_map.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/ff_map.v) | iCE40 触发器映射模板，`$_DFF_*`/`$_SDFF_*` → `SB_DFF*` |
| [techlibs/ice40/dsp_map.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/dsp_map.v) | iCE40 DSP 映射模板，`$__MUL16X16` → `SB_MAC16` |
| [techlibs/ice40/brams_map.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/brams_map.v) | iCE40 块 RAM 映射模板，`$__ICE40_RAM4K_` → `SB_RAM40_4K*` |
| [techlibs/ice40/brams.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/brams.txt) | iCE40 块 RAM 几何描述，供 `memory_libmap` 匹配存储器到 RAM |
| [techlibs/common/synth.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc) | 通用 `synth`（参照系，对比厂商流程多了什么） |

## 4. 核心概念与源码讲解

### 4.1 厂商 ScriptPass 的共同骨架与扩展点

#### 4.1.1 概念说明

通用 `synth`（[techlibs/common/synth.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc)）的阶段很简单：`begin`（hierarchy）→ `coarse`（proc/opt/fsm/share/memory -nomap）→ `fine`（memory_map/opt/techmap/abc）→ `check`。它停在「通用门级原语」上，不知道任何具体芯片。

厂商专属 ScriptPass（`synth_ice40`、`synth_xilinx`）的本质**仍是** u4-l2 讲的 ScriptPass：自身不实现任何综合算法，只用 `script()` 列出子 pass。它相对通用 `synth` 多做的，是把「平台映射知识」插进这条 lowering 流水线。可归纳为四类扩展点：

1. **起点注册物理原语**：在 `begin` 阶段 `read_verilog -lib -specify +/<vendor>/cells_sim.v`，把厂商原语注册成带定时信息的黑盒（u8-l1 的「黑盒+定时」加载路径）。
2. **插入平台映射阶段**：DSP、BRAM/分布式 RAM、算术进位链、FF 合法化——这些通用 `synth` 完全没有。
3. **平台相关 LUT 打包/优化**：`opt_lut -tech ice40`、`opt_lut_ins -tech xilinx`、`xilinx_dffopt`、`xilinx_srl` 等。
4. **终极落名 + 厂商格式输出**：结尾 `techmap -map +/<vendor>/cells_map.v` 把最后的 `$lut`/`$dff` 落成 `SB_LUT4`/`LUT6` 等具体名，再 `write_blif`/`write_edif`/`write_json`。

#### 4.1.2 核心流程

两条厂商流程都遵循一条「抽象逐级下降」的骨架，平台映射阶段按依赖关系穿插其中：

```
begin        读 cells_sim.v(-lib -specify) 注册原语；hierarchy；proc
prepare/flatten   行为级整理、可选展平
coarse       抽象层优化（opt/fsm/share/wreduce/peepopt）；memory -nomap 保留 $mem
map_dsp      乘法器 → DSP 原语（mul2dsp.v + <vendor>_dsp_map.v + <vendor>_dsp）
map_memory/  $mem → 块 RAM / 分布式 RAM（memory_libmap + brams/lutrams map）
map_ram
map_ffram    memory_map：剩余零散存储器降成 FF+逻辑
map_gates/   算术进位（<vendor> arith_map.v）、opt
fine
map_ffs      dfflegalize 把 $dff 约束成器件支持的 FF 子集；ff_map.v 落到 DFF 原语
map_luts     abc9/abc 做 LUT 映射（读 abc9_model.v 提供定时）；<vendor> 专属 LUT 优化
map_cells    techmap cells_map.v：终极落名（$lut → SB_LUT4/LUT6）
finalize     时钟缓冲/IO 缓冲插入（Xilinx 专有）
check        hierarchy -check；stat；blackbox 检查
写文件        write_blif / write_edif / write_json
```

**顺序背后的依赖链**（这是本讲最关键的理解点）：

- **DSP 必须在 `alumacc`/`memory` 之前**。DSP 原语（DSP48/SB_MAC16）内部带有流水线寄存器，`<vendor>_dsp` 会把乘法器前后的寄存器「吸收」进 DSP。因此必须先用 `memory_dff` 把存储器端口寄存器预留出来（避免被 DSP 抢走），再做 DSP，最后才让普通算术走 `alumacc`。源码注释也点明了这一点：`memory_dff` ——「DSP will merge registers, reserve memory port registers first」。
- **块 RAM（`map_memory`/`map_ram`）必须在 `memory_map`（`map_ffram`）之前**。`memory_libmap` 负责把大存储器映射成块 RAM 原语，`memory_map` 再把剩下的零散小存储器降成 FF+逻辑。libmap 在前才能「先挑大块」，否则大存储器会被拆成成千上万个 FF。
- **FF 合法化（`map_ffs`）必须在 LUT 映射（`map_luts`）之前**。`dfflegalize` 把宽泛的 `$dff` 家族约束成器件实际支持的子集，`techmap ff_map.v` 落成具体 DFF 原语；之后 abc/abc9 才在纯组合逻辑上做 LUT 映射。
- **终极 `cells_map.v`（`map_cells`）在最后**。此时网表只剩 `$lut`（abc 的产物）和少量未落名的 `$_` 原语，最后一道 techmap 把它们落到厂商物理名。

#### 4.1.3 源码精读

两个 pass 的类骨架完全同构：`ScriptPass` 子类 + `on_register()` 设 constpad 默认值 + `help()` 打印 + `execute()` 解析参数 + `script()` 编排。

iCE40 pass 的类定义与构造，命令名 `synth_ice40`：[techlibs/ice40/synth_ice40.cc:28-30](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L28-L30)。

`on_register()` 在注册时把三个器件（hx/lp/u）的 abc9 默认 `-W`（LUT 划分窗口宽度）写进全局 `constpad`：[techlibs/ice40/synth_ice40.cc:32-37](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L32-L37)。Xilinx 对称，只设 xc7 的默认：[techlibs/xilinx/synth_xilinx.cc:33-37](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L33-L37)。这些值在 `map_luts` 阶段被读出传给 `abc9`。

`script()` 入口（iCE40）：[techlibs/ice40/synth_ice40.cc:300-300](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L300-L300)。它先根据 `device_opt` 拼出 `-D ICE40_HX/LP/U` 宏传给后续 `read_verilog`，再用一连串 `check_label` 分阶段。

Xilinx pass 类定义：[techlibs/xilinx/synth_xilinx.cc:29-31](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L29-L31)。其 `script()` 在 [techlibs/xilinx/synth_xilinx.cc:340-340](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L340-L340)。

#### 4.1.4 代码实践

**实践目标**：用 `help` 直接对比「通用 synth」与「厂商 synth」的阶段标签，直观看出厂商流程多了哪些平台阶段。

**操作步骤**（命令行，假设已按 [u1-l2](u1-l2-build-and-run.md) 构建出 `./build/yosys`）：

```bash
./build/yosys -h synth            > synth.txt
./build/yosys -h synth_ice40      > ice40.txt
./build/yosys -h synth_xilinx     > xilinx.txt
```

`-h <命令>` 等价于在 shell 里敲 `help <命令>`，会调用该 pass 的 `help()` 并触发 `help_script()`（u4-l2 讲的双模式），把 `script()` 里所有 `run(...)` 以命令清单形式打印出来。

**需要观察的现象**：`synth.txt` 只有 `begin/coarse/fine/check` 四个标签；`ice40.txt` 多出 `flatten/map_ram/map_ffram/map_gates/map_ffs/map_luts/map_cells` 等标签；`xilinx.txt` 多出 `prepare/map_dsp/coarse/map_memory/map_ffram/fine/map_cells/map_ffs/map_luts/finalize`。

**预期结果**：清单里每个命令前都没有「(if ...)」注释的就是默认必跑的，带注释的是受选项控制的。这正好印证 4.1.1 的四类扩展点。

> 若本地尚未构建 yosys，本步骤为「待本地验证」；也可直接对照源码 `script()` 人工列出标签。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `synth_xilinx` 的 `help()` 开头会强调「does not operate on partly selected designs」？提示：看 `execute()` 里的检查。

**参考答案**：厂商流程内部大量使用 `select a:mul2dsp`、`select -clear`、`setattr` 等会改写全局选择栈的命令（见 [techlibs/xilinx/synth_xilinx.cc:444-448](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L444-L448)），且 `map_dsp`/`map_memory` 需要看到完整设计才能正确做 DSP/BRAM 推断。因此入口处 `if (!design->full_selection()) log_cmd_error(...)`（[techlibs/xilinx/synth_xilinx.cc:326-327](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L326-L327)）强制要求全选。

**练习 2**：`-run` 选项的 `from:to` 是怎么落到代码里的？

**参考答案**：`execute()` 把 `from:to` 拆成 `run_from`/`run_to` 两个字符串传给 `run_script(design, run_from, run_to)`（[techlibs/ice40/synth_ice40.cc:184-191](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L184-L191) 与 [295-295](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L295-L295)）；ScriptPass 框架据此控制每个 `check_label` 的 `block_active`，只开闸 `from`~`to` 范围内的阶段。

### 4.2 synth_ice40：iCE40 综合流程精读

#### 4.2.1 概念说明

iCE40 是 Lattice 的低成本 FPGA 家族（HX/LP/U 三个子系列），其物理原语全部以 `SB_` 开头（SB_LUT4、SB_DFF、SB_RAM40_4K、SB_MAC16、SB_CARRY）。`synth_ice40` 是这些原语里最简洁的厂商流程之一，适合作为读懂「厂商 ScriptPass」的入门样本。它的设计取舍很务实：默认开启 abc9（新的 LUT 映射流程）、默认展平、BRAM 默认推断，绝大多数行为都能用布尔开关控制。

#### 4.2.2 核心流程

`script()` 把流程切成 10 个标签，对照 4.1.2 的通用骨架可以清楚看到平台阶段插在哪：

| 标签 | 关键命令 | 作用 |
|------|----------|------|
| `begin` | `read_verilog -lib -specify +/ice40/cells_sim.v`；`hierarchy`；`proc` | 注册 SB_ 原语黑盒+定时；建层次；行为级降级 |
| `flatten` | `flatten`；`tribuf -logic`；`deminout` | 默认展平（`-noflatten` 关闭） |
| `coarse` | `opt`/`fsm`/`wreduce`/`peepopt`/`share`；DSP 块；`alumacc`；`memory -nomap` | 抽象优化；可选 DSP 推断；保留 `$mem` |
| `map_ram` | `memory_libmap -lib brams.txt -lib spram.txt`；`techmap brams_map.v spram_map.v`；`ice40_braminit` | 大存储器 → SB_RAM40_4K |
| `map_ffram` | `opt -fast`；`memory_map`；`opt -undriven` | 剩余存储器降成 FF+逻辑 |
| `map_gates` | `ice40_wrapcarry`；`techmap techmap.v arith_map.v` | 算术进位（SB_CARRY）包装与映射 |
| `map_ffs` | `dfflegalize`；`techmap ff_map.v`；`simplemap`；`ice40_opt -full` | `$dff` → SB_DFF* |
| `map_luts` | `abc9`（默认）/ `abc -lut 4`；`ice40_wrapcarry -unwrap`；`techmap ff_map.v`；`opt_lut -tech ice40` | LUT 映射与 iCE40 专属打包 |
| `map_cells` | `techmap +/ice40/cells_map.v` | 终极落名 |
| `check` | `autoname`；`hierarchy -check`；`stat`；`check -noinit` | 检查与统计 |

#### 4.2.3 源码精读

**起点注册原语**：`begin` 阶段读 cells_sim.v，宏 `-D ICE40_HX/LP/U` 控制仿真模型里的器件差异：[techlibs/ice40/synth_ice40.cc:315-320](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L315-L320)。

**DSP 推断块（仅 `-dsp` 时）**：这是 4.1.2 所述「DSP 必须先于普通算术」的实物证据。注意它被放在 `coarse` 内、`alumacc` 之前：[techlibs/ice40/synth_ice40.cc:347-360](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L347-L360)。关键调用：

```text
memory_dff ...                // 先预留存储器端口寄存器
wreduce t:$mul                // 缩窄乘法器
techmap -map +/mul2dsp.v -map +/ice40/dsp_map.v -D DSP_NAME=$__MUL16X16 ...
select a:mul2dsp              // 选中刚标好的乘法器
ice40_dsp                     // 真正的 DSP 推断/寄存器吸收
chtype -set $mul t:$__soft_mul // 太小的乘法退回 $mul，留给 LUT 实现
```

模板 `dsp_map.v` 把内部单元 `$__MUL16X16` 直接替换成 `SB_MAC16`：[techlibs/ice40/dsp_map.v:1-34](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/dsp_map.v#L1-L34)。

**块 RAM 映射**：`map_ram` 用 `memory_libmap` 匹配 `brams.txt`（声明 `$__ICE40_RAM4K_` 的几何：11 位地址、宽度 2/4/8/16、代价 64，见 [techlibs/ice40/brams.txt:1-23](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/brams.txt#L1-L23)），再用 `brams_map.v` 把 `$__ICE40_RAM4K_` 落成 `SB_RAM40_4K*`：[techlibs/ice40/synth_ice40.cc:367-381](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L367-L381)。

**FF 合法化**：`dfflegalize` 把 `$dff` 家族约束到 iCE40 实际支持的 FF 子集（`-nodffe` 时更窄），随后 `techmap ff_map.v` 落到 `SB_DFF*`：[techlibs/ice40/synth_ice40.cc:404-414](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L404-L414)。`ff_map.v` 是一张极简的「`$_DFF_*` → SB_DFF*」对照表，例如 `\$_DFF_P_` → `SB_DFF`：[techlibs/ice40/ff_map.v:1-2](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/ff_map.v#L1-L2)。

**LUT 映射（默认 abc9）**：先读 iCE40 专属的 `abc9_model.v`（提供 SB_ 单元的定时模型给 abc9），再读 constpad 里的 `-W`，最后 `abc9`：[techlibs/ice40/synth_ice40.cc:433-449](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L433-L449)。随后 `ice40_wrapcarry -unwrap` 把 `map_gates` 阶段为进位链临时包装的单元解包，`opt_lut -tech ice40` 做 iCE40 专属 LUT 打包。

**终极落名**：`map_cells` 的 `techmap +/ice40/cells_map.v`：[techlibs/ice40/synth_ice40.cc:456-463](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L456-L463)。

#### 4.2.4 代码实践

**实践目标**：观察 `synth_ice40` 在不同开关下输出的原语差异，建立「选项→阶段→原语」的直觉。

**操作步骤**：

```bash
# 准备一个最小组合+时序设计
cat > top.v <<'EOF'
module top(input clk, input a, input b, output reg q);
  always @(posedge clk) q <= a & b;
endmodule
EOF

./build/yosys -p "read_verilog top.v; synth_ice40 -json o1.json; stat"
./build/yosys -p "read_verilog top.v; synth_ice40 -nodffe -json o2.json; stat"
```

**需要观察的现象**：第一次 `stat` 里出现 `SB_DFF`（或 `SB_DFFE`）；加 `-nodffe` 后 DFF 类型收窄（不再用带使能的 `SB_DFFE*`），逻辑门变成 `SB_LUT4`。

**预期结果**：`-nodffe` 改变了 `map_ffs` 里 `dfflegalize` 约束的单元集合（[techlibs/ice40/synth_ice40.cc:406-409](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L406-L409)），最终网表里出现的 SB_DFF 子类随之不同。这印证了「选项→脚本分支→输出原语」的链路。

> 未本地构建时为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`map_gates` 里为什么要先 `ice40_wrapcarry` 再 `techmap ... arith_map.v`，而 `map_luts` 里又 `ice40_wrapcarry -unwrap`？

**参考答案**：iCE40 的进位链（SB_CARRY）需要把算术进位「包装」成特殊单元才能被正确映射，`wrapcarry` 在算术映射前把进位结构包起来交给 `arith_map.v`；等 abc9 做完 LUT 映射、不再需要进位包装时，`-unwrap` 把没被吸收的包装单元解包回普通逻辑（[techlibs/ice40/synth_ice40.cc:392-402](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L392-L402) 与 [450-451](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L450-L450)）。

**练习 2**：默认流程里乘法器会进 DSP 吗？

**参考答案**：不会。DSP 块整个被 `if (help_mode || dsp)` 守卫（[techlibs/ice40/synth_ice40.cc:347-347](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L347-L347)），只有 `-dsp`（针对 iCE40 UltraPlus）才启用；默认乘法走 `alumacc` → techmap → LUT/进位实现。

### 4.3 synth_xilinx：Xilinx 流程与多 family 参数化

#### 4.3.1 概念说明

Xilinx 的厂商流程比 iCE40 复杂得多，核心原因是它要**用一个 ScriptPass 覆盖十几个器件家族**（从 Spartan-2/Virtex 到 UltraScale+），而这些家族的 LUT 宽度、DSP 位宽、BRAM 容量、是否支持 SRL/分布式 RAM 都不同。`synth_xilinx` 的解法是：在 `execute()` 里根据 `-family` 选项计算 `lut_size`/`widelut_size`，并在 `script()` 里用 `family` 字符串动态拼接出**不同的 techmap 模板路径与参数**。这是「数据驱动 + 运行时分支」的典型范式。

#### 4.3.2 核心流程

阶段标签：`begin`/`prepare`/`map_dsp`/`coarse`/`map_memory`/`map_ffram`/`fine`/`map_cells`/`map_ffs`/`map_luts`/`finalize`/`check`。相对 iCE40 多出了 `prepare`（更细的行为级整理，含 `muxpack`/`pmux2shiftx`/`wreduce`）、`finalize`（时钟缓冲 `clkbufmap`、IO 缓冲 `iopadmap`），并且 `map_dsp` 在 `coarse` **之前**。

LUT 宽度按 family 分类（这是 Xilinx 流程的「骨架参数」）：

| family | lut_size | widelut_size |
|--------|----------|--------------|
| xcup / xcu（UltraScale(+)) | 6 | 9 |
| xc7 / xc6v / xc5v / xc6s | 6 | 8 |
| xc4v / xc3s* / xc2v* | 4 | 8 |
| xcve / xcv（Virtex/Spartan-2） | 4 | 6 |

数学上，一个 LUT 用 INIT 位串编码真值表，位宽为 \( 2^{\text{WIDTH}} \)（WIDTH 输入的函数表共 \( 2^{\text{WIDTH}} \) 行）。\[ \text{INIT 位宽} = 2^{\text{WIDTH}} \] LUT6 的 INIT 为 64 位；iCE40 的 SB_LUT4 是 16 位（见 4.4）。

#### 4.3.3 源码精读

**family → LUT 宽度**：`execute()` 里一长串 `if/else if` 把 family 映射成 `lut_size`/`widelut_size`，非法 family 直接报错：[techlibs/xilinx/synth_xilinx.cc:291-313](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L291-L313)。注意 LUT4 家族会强制 `nosrl=true`（移位寄存器推断尚未支持）：[techlibs/xilinx/synth_xilinx.cc:318-321](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L318-L321)。

**起点注册原语**：`begin` 读 `cells_sim.v` 与 `cells_xtra.v`（额外的原语黑盒）：[techlibs/xilinx/synth_xilinx.cc:346-354](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L346-L354)。

**map_dsp（在 coarse 之前）**：这是 Xilinx 流程最体现「family 分支」的地方。对每个 family 调用**不同的** `{family}_dsp_map.v`，并传不同的 `DSP_A_MAXWIDTH` 等参数（例如 xc7 用 25×18 的 `DSP_NAME=$__MUL25X18`，xcu 用 27×18）：[techlibs/xilinx/synth_xilinx.cc:395-454](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L395-L454)。xc7 分支还特意设了 `DSP_A_MAXWIDTH_PARTIAL=18`，注释说明是为了利用 `(PCOUT << 17) -> PCIN` 专用级联链（[techlibs/xilinx/synth_xilinx.cc:426-434](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L426-L434)）。收尾 `xilinx_dsp -family <family>` 做真正的 DSP48 推断。

**map_memory（BRAM/分布式 RAM/URAM）**：同样是 family 大分支，为每个 family 选不同的 `lutrams_<f>.txt`/`brams_<f>.txt`/`brams_<f>_map.v`，并用 `-D HAS_SIZE_36`/`HAS_CASCADE`/`HAS_BE` 等宏表达该家族能力差异：[techlibs/xilinx/synth_xilinx.cc:466-558](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L466-L558)。`-nobram`/`-nolutram`/`-uram` 三个开关映射成 `-no-auto-block`/`-no-auto-distributed`/`-no-auto-huge` 传给 `memory_libmap`。

**map_ffs（family 分支）**：不同家族支持不同的 FF 类型，`dfflegalize` 的约束也随之不同；例如 xc6s 用 `r`（复位优先级）参数，xc7 用 `01`：[techlibs/xilinx/synth_xilinx.cc:626-632](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L626-L632)。

**map_luts**：`abc9`（仅 xc7 且需显式 `-abc9`，[techlibs/xilinx/synth_xilinx.cc:648-668](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L648-L668)）或默认 `abc -luts ...`（[669-689](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L669-L689)）。注意 `abc` 的 `-luts` 参数随 `lut_size`/`widelut_size` 动态拼接，LUT6 家族用 `2:2,3,6:5[,10,20[,40]]`（允许用 MUXF 把多个 LUT 拼成宽 LUT）。随后 `xilinx_srl`（移位寄存器推断）、`techmap lut_map.v cells_map.v`、`xilinx_dffopt`、`opt_lut_ins -tech xilinx`（[698-707](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L698-L707)）。

**finalize**：`clkbufmap`（自动插 BUFG 时钟缓冲）、可选 `extractinv`（给 ISE 流程提取反相器）：[techlibs/xilinx/synth_xilinx.cc:710-716](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L710-L716)。

#### 4.3.4 代码实践

**实践目标**：对比同一设计在 LUT4 家族与 LUT6 家族下的 LUT 数量与类型差异，体会 family 参数化的影响。

**操作步骤**：

```bash
cat > wide.v <<'EOF'
module wide(input [7:0] a, input [7:0] b, output [8:0] y);
  assign y = a + b;          // 8 位加法
endmodule
EOF

./build/yosys -p "read_verilog wide.v; synth_xilinx -family xc7  -json xc7.json;  stat -tech xilinx"
./build/yosys -p "read_verilog wide.v; synth_xilinx -family xcve -json xcv.json; stat -tech xilinx"
```

**需要观察的现象**：xc7（LUT6）综合后 `stat` 里出现 `LUT6`/`CARRY4` 等 7 系列原语；xcve（LUT4，Virtex/Spartan-2）出现 LUT2/LUT3/LUT4 等更窄的 LUT，且日志会有一条「Shift register inference not yet supported for family xcve」的警告（因为 LUT4 家族被强制 `nosrl=true`）。

**预期结果**：family 直接决定了 `map_luts` 里 `abc` 的 `-luts` 参数与 `lut_map.v` 的 `LUT_WIDTH`（[techlibs/xilinx/synth_xilinx.cc:698-700](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L698-L700)），因此 LUT 类型和数量不同。

> 未本地构建时为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Xilinx 的 `map_dsp` 放在 `coarse` 之前，而 iCE40 的 DSP 块放在 `coarse` 之内？

**参考答案**：两者目的一样（让 DSP 抢先吸收乘法器寄存器，再让普通算术走 alumacc），只是阶段命名与组织不同。Xilinx 单独给 `map_dsp` 一个标签使其能用 `-run` 精确控制；iCE40 因为 DSP 是可选项、整体更简单，就内嵌在 `coarse` 的 `if (dsp)` 块里（[techlibs/ice40/synth_ice40.cc:347-360](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L347-L360)）。

**练习 2**：`memory_libmap` 阶段那些 `-D HAS_SIZE_36`/`HAS_CASCADE` 宏最终被谁消费？

**参考答案**：它们通过 `read_verilog -D` 影响随后 `techmap -map brams_<family>_map.v` 加载的模板里的 `\`ifdef` 分支，使同一份映射模板能按家族能力展开成不同拓扑（级联、36Kb 模式等），见 [techlibs/xilinx/synth_xilinx.cc:466-558](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L466-L558)。

### 4.4 平台映射脚本：从 `$` 单元到物理原语

#### 4.4.1 概念说明

4.2、4.3 反复出现 `techmap -map <vendor>/xxx_map.v`。这些 `_map.v` 就是 u8-l1 讲的「映射模板」——用普通 Verilog 模块描述「内部单元 → 厂商原语」的替换规则（标志是 `_TECHMAP_REPLACE_`、`techmap_celltype`、`_TECHMAP_FAIL_` 等 techmap 专用属性）。厂商流程之所以能把所有平台知识外置成数据文件，正是因为 u6-l5 讲过：techmap 是一台不懂语义的模板替换机，新增映射无需改 C++。本模块读三个最典型的模板。

#### 4.4.2 核心流程

techmap 套用模板的统一逻辑（复习 u6-l5）：

```
对每个 cell：
  按其 type 在 -map 指定的模板库里找同名模块（或 techmap_celltype 声明的别名）
  绑定参数/端口（含 _TECHMAP_CONSTMSK_/CONSTVAL_ 等自动注入的常量位信息）
  若模板设了 wire _TECHMAP_FAIL_ = 1 → 放弃此模板，试下一个
  否则把模板内容内联进当前模块，删除原 cell
  循环到不动点
```

模板里常用三种 techmap 设施：`_TECHMAP_REPLACE_`（整体替换，端口自动对接）、`_TECHMAP_FAIL_`（条件放弃）、`_TECHMAP_CONSTMSK_<port>_`/`_TECHMAP_CONSTVAL_<port>_`（告诉模板某端口哪些位是常数，供其做特化）。

#### 4.4.3 源码精读

**iCE40 cells_map.v：`$lut` → `SB_LUT4`**。模板对 `WIDTH` 做 `generate if`，把 1~4 输入的 `$lut` 映射到一个 SB_LUT4，`WIDTH>4` 时 `_TECHMAP_FAIL_`（SB_LUT4 最多 4 输入）：[techlibs/ice40/cells_map.v:1-32](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/cells_map.v#L1-L32)。

WIDTH=1 时 INIT 复制是一个值得理解的细节：

```verilog
localparam [15:0] INIT = {{8{LUT[1]}}, {8{LUT[0]}}};
SB_LUT4 #(.LUT_INIT(INIT)) _TECHMAP_REPLACE_ (.O(Y),
    .I0(1'b0), .I1(1'b0), .I2(1'b0), .I3(A[0]));
```

SB_LUT4 的 16 位 INIT 由 `{I3,I2,I1,I0}` 索引（共 \( 2^4=16 \) 项）。这里把 I0~I2 接 0，只有 I3 接 A[0]，于是只有索引 0（A[0]=0）和索引 8（A[0]=1）两项有效。`{8{LUT[1]},8{LUT[0]}}` 让 INIT[0]=LUT[0]、INIT[8]=LUT[1]，正好把 1 输入函数编码进 16 位表。这是「把窄函数嵌入宽 LUT」的标准技巧。

**Xilinx cells_map.v：比 iCE40 复杂得多**。它不只做 `$lut`（LUT 映射在 `lut_map.v`），而是承载三件事：

1. 移位寄存器 `__SHREG_` → `SRL16E`/`SRLC32E`（按 DEPTH 分档，>32 时级联多个 SRLC32E 并用 MUXF7 选择，[techlibs/xilinx/cells_map.v:21-140](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/cells_map.v#L21-L140)）。
2. `__XILINX_MUXF78` → 物理硬 mux `MUXF7`/`MUXF8`（带 `_TECHMAP_CONNMAP_`/`CONSTMSK_` 优化：当两个输入接同一根线或选择位为常数时直接用 assign 省掉一个 mux，[techlibs/xilinx/cells_map.v:333-364](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/cells_map.v#L333-L364)）。
3. `__XILINX_SHIFTX`（仅 `-widemux` 启用时编译，用 `\`ifdef MIN_MUX_INPUTS` 守卫）——把宽多路器映射到硬 mux 资源（[techlibs/xilinx/cells_map.v:142-331](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/cells_map.v#L142-L331)）。注意有个有趣的「反向」模板用 `(* techmap_celltype = "$__XILINX_SHIFTX" *)` 把内部单元还原回 `$shiftx`（[techlibs/xilinx/cells_map.v:285-301](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/cells_map.v#L285-L301)），用于无法落硬 mux 时的兜底。

**ff_map.v 与 dsp_map.v 的共同写法**：一行一个模块，用 `_TECHMAP_REPLACE_` 直接替换。如 `\$_DFF_P_` → `SB_DFF`（[techlibs/ice40/ff_map.v:1-2](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/ff_map.v#L1-L2)），`$__MUL16X16` → `SB_MAC16`（[techlibs/ice40/dsp_map.v:1-34](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/dsp_map.v#L1-L34)）。它们都是「查表式」的纯数据映射。

#### 4.4.4 代码实践

**实践目标**：用 `techmap -map` 手动套用单个模板，观察替换前后 RTLIL 的变化，理解模板是「无语义的替换」。

**操作步骤**：

```bash
cat > one.v <<'EOF'
module one(input [3:0] a, output y);
  assign y = &a;       // 4 输入与归约，综合后是 1 个 $lut
endmodule
EOF

# 只跑到 abc9 之前，看 $lut；再手动套 cells_map.v 看 SB_LUT4
./build/yosys -p "read_verilog one.v; synth_ice40 -run begin:map_ffs; dump"
./build/yosys -p "read_verilog one.v; synth_ice40 -run begin:map_cells; dump"
```

**需要观察的现象**：第一次 `dump`（停在 map_cells 之前）能看到 `$lut` 单元；第二次（跑完 map_cells）`$lut` 被替换成 `SB_LUT4`，带上了 16 位 `LUT_INIT` 参数。

**预期结果**：印证 `map_cells` 的 `techmap +/ice40/cells_map.v`（[techlibs/ice40/synth_ice40.cc:456-463](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L456-L463)）就是把 `$lut` 落成 `SB_LUT4` 的最后一步。

> 未本地构建时为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`ff_map.v` 每行末尾都有 `wire _TECHMAP_REMOVEINIT_Q_ = 1;`，它起什么作用？

**参考答案**：这是 techmap 的专有约定，告诉 techmap 在替换 DFF 时**移除** Q 端口上原有的 init 属性（init 值已经搬进 SB_DFF*/FDRE 的 `INIT` 参数里了），避免属性在替换后悬挂或冲突。

**练习 2**：iCE40 的 `$lut` 模板对 WIDTH>4 设 `_TECHMAP_FAIL_`。那 abc9 产生的超过 4 输入的逻辑怎么处理？

**参考答案**：abc9 在 `map_luts` 阶段就已经用 `-W`（默认来自 constpad，如 hx=250）把逻辑划分成 ≤4 输入的 LUT（SB_LUT4 只有 4 输入），所以到 `map_cells` 套 `cells_map.v` 时不会再出现 WIDTH>4 的 `$lut`；`_TECHMAP_FAIL_` 是兜底安全网。

## 5. 综合实践

把本讲三块知识串起来：综合一个**含 BRAM 的 iCE40 设计**，对照 `synth_ice40.cc` 找出它调用的 LUT/BRAM 映射子 pass，并解释调用顺序。

### 步骤 1：准备设计（含一块 256×8 的存储器）

```verilog
// ram_top.v
module ram_top #(parameter W=8, D=256) (
    input            clk,
    input            wen,
    input      [7:0] addr,
    input  [W-1:0]   wdata,
    output [W-1:0]   rdata
);
    reg [W-1:0] mem [0:D-1];
    reg [W-1:0] rdata_r;
    always @(posedge clk) begin
        if (wen) mem[addr] <= wdata;
        rdata_r <= mem[addr];
    end
    assign rdata = rdata_r;
endmodule
```

### 步骤 2：综合并观察阶段日志

```bash
./build/yosys -p "read_verilog ram_top.v; synth_ice40 -json ram.json" 2>&1 | tee log.txt
./build/yosys -p "read_verilog ram_top.v; synth_ice40 -json ram.json; stat"
```

`synth_ice40` 执行时会把每个标签和子 pass 打印到日志，形如 `7. Executing SYNTH_ICE40 pass.` 下面的 `8.1. Executing ...`。对照 [techlibs/ice40/synth_ice40.cc:300-502](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L300-L502) 逐条核对。

### 步骤 3：定位 BRAM 映射子 pass 与顺序解释

针对这块存储器，相关子 pass 形成的调用链（按源码顺序）：

1. **`memory -nomap`**（`coarse`，[363-363](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L363-L363)）：把行为级 `reg mem[]` 收集成 `$mem`，但**故意不展开**，留给后面的块 RAM 推断。
2. **`memory_libmap -lib +/ice40/brams.txt`**（`map_ram`，[378-378](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L378-L378)）：用 `brams.txt` 声明的 `$__ICE40_RAM4K_` 几何（256×8 = 2048 位 ≤ 4096 位，匹配）把 `$mem` 映射成 `$__ICE40_RAM4K_`。**必须在 `memory_map` 之前**，否则这块 RAM 会被拆成 FF。
3. **`techmap -map +/ice40/brams_map.v`**（`map_ram`，[379-379](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L379-L379)）：把 `$__ICE40_RAM4K_` 落成物理 `SB_RAM40_4K*`（模板见 [techlibs/ice40/brams_map.v:1-70](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/brams_map.v#L1-L70)，含地址线位序重排与 INIT 切片）。
4. **`ice40_braminit`**（`map_ram`，[380-380](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L380-L380)）：iCE40 专属 pass，处理块 RAM 的初始化内容（`$meminit` → SB_RAM40_4K 的 INIT_0..INIT_F）。
5. **`memory_map`**（`map_ffram`，[386-386](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L386-L386)）：此时大块 RAM 已是 SB_RAM40_4K，这里只处理任何剩余的零散小存储器（本例没有）。

### 步骤 4：验证 LUT 映射子 pass

存储器之外的纯逻辑（本例几乎没有）与寄存器走另一条链：`dfflegalize`（`map_ffs`，[407-409](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L407-L409)）约束 FF → `techmap ff_map.v` 落 `SB_DFF` → `abc9`（`map_luts`，[445-445](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L445-L445)）把组合逻辑映射成 `$lut` → `techmap cells_map.v`（`map_cells`，[461-461](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L461-L461)）落 `SB_LUT4`。

### 预期结果

- `stat` 输出里出现 `SB_RAM40_4K`（1 块，因 256×8=2Kb ≤ 4Kb）、若干 `SB_DFF`、（若有逻辑）若干 `SB_LUT4`。
- 用 `-nobram` 再跑一遍对照：`map_ram` 因 `-no-auto-block` 跳过块 RAM 推断（[375-376](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc#L375-L376)），存储器改由 `memory_map` 降成 256×8 个 `SB_DFF`——单元数爆炸，直观证明「libmap 必须在 map 之前」的必要性。

> 若本地未构建 yosys，上述运行结果为「待本地验证」；但子 pass 调用顺序与行号可直接从 [techlibs/ice40/synth_ice40.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc) 静态核对。

## 6. 本讲小结

- 厂商专属 ScriptPass（`synth_ice40`/`synth_xilinx`）本质仍是 u4-l2 的 ScriptPass，自身不写算法，只把「平台映射知识」插进通用 lowering 流水线，扩展点有四类：注册原语、平台映射阶段、LUT 打包优化、终极落名+厂商格式输出。
- 阶段顺序由依赖链决定：DSP 先于普通算术（吸收寄存器）、块 RAM（`memory_libmap`）先于 `memory_map`（先挑大块）、FF 合法化先于 LUT 映射、`cells_map.v` 终极落名在最后。
- `synth_ice40` 流程含 `map_ram/map_ffram/map_gates/map_ffs/map_luts/map_cells` 等平台阶段，默认开 abc9，DSP 需 `-dsp` 显式启用。
- `synth_xilinx` 用 `family` 选项做运行时参数化：family 决定 `lut_size`/`widelut_size`，并动态选择 `{family}_dsp_map.v`/`brams_<family>_map.v` 与 `dfflegalize` 约束，还多出 `prepare`/`finalize` 阶段。
- `constpad` 是存放编译期默认参数值的全局表，`on_register()` 写入各器件的 abc9 默认 `-W`。
- `cells_map.v`/`ff_map.v`/`dsp_map.v` 等 `_map` 模板是纯数据的「内部单元→厂商原语」替换规则，靠 `_TECHMAP_REPLACE_`/`_TECHMAP_FAIL_`/`_TECHMAP_CONSTMSK_` 等 techmap 属性驱动，新增映射无需改 C++。

## 7. 下一步学习建议

- **读完 iCE40/Xilinx 的全部 pass**：本讲只点了主链。建议打开 [techlibs/ice40/synth_ice40.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/synth_ice40.cc) 的 `script()`，对每条不熟悉的子 pass（如 `ice40_braminit`、`ice40_opt`、`ice40_wrapcarry`）去 `passes/` 或 `techlibs/ice40/*.cc` 读它的实现。
- **看其他厂商流程**：`techlibs/` 下还有 `ecp5`、`gowin`、`anlogic`、`efinix`、`intel` 等，它们都是同构的 ScriptPass。对比 `synth_ecp5` 与 `synth_ice40`（同为 Lattice 系）能加深对「平台差异如何外置成数据」的理解。
- **进入扩展层**：[u9-l1](u9-l1-write-custom-pass.md) 将教你编写自定义 Pass。届时你可以尝试模仿 `ice40_opt.cc`，写一个针对自己平台的简单优化 pass。
- **接续高级内部机制**：[u10-l1](u10-l1-sat-formal-verification.md) 讲 SAT/形式验证，与本讲的「综合到网表」互补——前者证明设计性质，后者生成网表，两者共用同一套 RTLIL。
