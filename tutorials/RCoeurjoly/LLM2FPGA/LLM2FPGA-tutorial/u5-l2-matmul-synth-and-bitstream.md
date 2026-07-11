# matmul 端到端综合与比特流生成

## 1. 本讲目标

上一讲（u5-l1）我们让 Yosys + slang 把 CIRCT 导出的 SystemVerilog 读进来，落成 RTLIL。本讲把这条**后端**继续走到尽头：从 RTLIL/SystemVerilog 经 `synth_xilinx` 综合成映射网表 JSON，再经开源工具链 `nextpnr-xilinx` 布局布线成 FASM，最后经 Project X-Ray 的 `fasm2frames` + `xc7frames2bit` 生成可烧录的 `.bit` 比特流。

学完本讲，你应当能够：

- 说出开源比特流三段式 **JSON → FASM → bit** 每一段用的工具与其职责；
- 读懂 `flake.nix` 里 `mkSynthJson` / `mkXdc` / `mkFasm` / `mkBitstream` 四个函数如何用 `runCommand` 把工具包成可缓存派生，并串成 `matmulSelftestBitstream` 一条依赖链；
- 理解为什么**只有 matmul 能走到比特流**，而 141 倍超配的 TinyStories-1M 走不到（连接 u1-l4 的瓶颈结论）；
- 看懂 `matmul_selftest_top.sv` 这个「板级自测外壳」如何在综合期用常量函数算出期望值、上电后自我验证并用 LED 报告 pass/fail。

## 2. 前置知识

本讲面向已读完 u5-l1（Yosys + slang 前端）的读者。再补三个概念：

- **综合（synthesis）**：把 RTL（寄存器传输级，如 SystemVerilog 的 `always_ff`、`assign`）翻译成「目标芯片的基本单元」——LUT（查找表）、FF（触发器）、DSP、BRAM 等。Yosys 的 `synth_xilinx` 就是面向 Xilinx 7 系列的综合流程。
- **布局布线（Place & Route, PnR）**：把综合出的网表里的每个单元「摆」到芯片物理位置的 BEL 上，再「连线」走通片上网络。`nextpnr` 是开源 PnR 工具，`nextpnr-xilinx` 是其对 Xilinx 7 系列的版本（属 openXC7 项目）。
- **FASM（FPGA Assembly）**：一种人类可读的文本格式，逐条描述「要编程哪些熔丝/特性」。它是 PnR 结果与底层「帧（frame）」数据库之间的中间表示。
- **比特流（bitstream, `.bit`）**：最终可烧进 FPGA 配置存储的二进制文件。Xilinx 7 系列的比特流由 Project X-Ray（prjxray）项目逆向出的「帧数据库」拼接而成。
- **`runCommand`**：Nix 的轻量派生构造器，给一段 shell 命令、一个 `$out` 输出路径，就产出一个 `/nix/store/` 里的产物。本讲四个 `mk*` 函数都基于它（回顾 u3-l5）。

> 一句话定位：u5-l1 解决「把 SV 读进 Yosys」，本讲解决「把 Yosys 里的设计一路变成能烧的 `.bit`，并证明它在板子上能自检」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `flake.nix` | 全部后端逻辑都在这里：`mkSynthJson`（综合→JSON）、`mkXdc`（合并约束）、`mkFasm`（nextpnr→FASM）、`mkBitstream`（→bit），以及把它们串起来的 `matmulSelftestBitstream`。 |
| `fpga/rtl/matmul_selftest_top.sv` | 板级自测外壳：包住生成的 matmul DUT，上电播种输入、触发计算、比对期望值、用 LED 报告 pass/fail。 |
| `fpga/constraints/matmul_selftest.xdc` | 外壳的引脚约束：把 `SYS_CLK`/`SYS_RSTN`/3 个 LED 绑到具体封装管脚。 |
| `sim/test_vectors.json` | （参照）仿真用的黄金输入向量 a=[1..16]、b=[16..1]，点积为 816——与本讲外壳在综合期算出的 `EXPECTED` 殊途同归。 |

## 4. 核心概念与源码讲解

### 4.1 综合：`synth_xilinx` 把设计映射成 JSON（mkSynthJson）

#### 4.1.1 概念说明

`mkSynthJson` 是本段的入口：它读入 SystemVerilog（matmul 的生成 SV + 自测外壳），用 Yosys 的 `synth_xilinx` 流程把它综合成一张**已映射到 Xilinx 7 系列单元的 JSON 网表**。

这份 JSON 是一个关键的「分叉点」：

- 往下走物理实现：喂给 `nextpnr-xilinx` 做布局布线（本讲 4.2）；
- 往旁边走资源核算：喂给 `write_utilization_report.py` 统计 LUT/FF/DSP/BRAM（u5-l3）。

也就是说，同一份 `synth_xilinx -json` 产物，既是「要被摆上芯片的网表」，也是「资源报告的原料」。理解这点能串起 u5-l2 与 u5-l3。

> 注意与 u5-l1 的区别：u5-l1 的 `sv_to_il.sh` 用 `write_rtlil` 落成 `.il`，是「未映射」的技术网表；本讲 `synth_xilinx -json` 已经把单元映射成 `LUT6`/`FDRE`/`DSP48E1`/`RAMB36` 等 Xilinx 原语，是「已映射」网表。

#### 4.1.2 核心流程

`mkSynthJson` 组装出一段 Yosys 脚本 `run.ys`，步骤是：

1. **读设计**：用 slang 前端把生成的 matmul SV（来自降级链，见 u3-l4 的 `sources.f`）与自测外壳 `matmul_selftest_top.sv` 一起读进来。matmul 走的是 **svFilelist 模式**（直接读 `.sv`），而 TinyStories 走 **modelIl 模式**（读预处理过的 `.il`）——这是两者的关键差异之一。
2. `hierarchy -top matmul_selftest_top -check`：选定顶层、校验层次完整。
3. `proc`：把 `always`/`initial` 过程块展平成数据流。
4. `synth_xilinx -family xc7 -top matmul_selftest_top -noiopad -json $out`：完整综合并写出 JSON。
5. 执行 `yosys -m slang.so -s run.ys`，并校验 `$out` 真的生成了。

#### 4.1.3 源码精读

`mkSynthJson` 的函数签名与「二选一」断言——必须且只能在 `modelIl`（RTLIL 入口）与 `svFilelist`（SV 文件列表入口）之间选一个：

[flake.nix:497-511](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L497-L511) — 定义入参与断言。`assertMsg (useModelIl != useSvFilelist)` 在两者都给或都不给时直接编译期报错，把误用挡在构建之前。

真正干活的 `runCommand` 体——读设计 + 综合写出 JSON：

[flake.nix:512-529](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L512-L529) — 组装 `run.ys`：先是 `inputScript`（根据模式选 slang 读法），再追加 `hierarchy -top ... -check` → `proc` → `synth_xilinx ... -json $out`，最后 `yosys -m slang.so -s run.ys` 执行，并校验产物存在。

`svFilelist` 模式下读设计的细节：逐行读 `sources.f`（跳过空行与 `#` 注释），拼成一条 `read_slang --threads 1 --no-proc <各 .sv> <topSv>` 命令：

[flake.nix:480-495](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L480-L495) — `appendReadSlangFromFilelist`。`--no-proc` 故意不在 slang 内做过程块处理，留给后面的显式 `proc`（与 u5-l1 的 `--no-proc` 哲学一致：把重活统一交给 synth 流程）。

`synth_xilinx` 三个关键开关：

- `-family xc7`：目标 7 系列（Kintex/Artix/Spartan），决定映射到哪一组原语；
- `-top matmul_selftest_top`：顶层模块；
- `-noiopad`：跳过自动 I/O 缓冲插入，把引脚处理交给 XDC 与下游（nextpnr）。

#### 4.1.4 代码实践

**目标**：确认 matmul 的综合入口走的是 svFilelist 模式，且产物是一份 JSON 网表。

**步骤**：

1. 打开 `flake.nix`，找到 `matmulSelftestJson`（约 681 行）：
   [flake.nix:681-686](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L681-L686) — 注意它传了 `svFilelist = "${matmulSv}/sources.f"`（**没**传 `modelIl`），`topName = "matmul_selftest_top"`，`topSv = matmulSelftestTop`。
2. 追问：`matmulSv` 从哪来？答案在 [flake.nix:244](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L244) —— `matmulSv = modelRegistry.matmul.pipeline.sv`，即降级链（u3-l4）导出的 SV 目录，里面有 `main.sv` 与 `sources.f`。

**观察现象**：综合产物路径形如 `/nix/store/...-matmul-selftest.json`，是一个 JSON 文件（不是 `.il`、不是 `.sv`）。

**预期结果**：构建成功，得到一份映射网表 JSON。

**待本地验证**：本讲未在沙箱内实跑；若执行 `nix build .#matmul-selftest-bitstream`（见 4.3），其依赖链里会出现 `matmul-selftest.json`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mkSynthJson` 要用 `assertMsg` 强制「`modelIl` 与 `svFilelist` 二选一」？

**参考答案**：这两个参数分别对应两条不同的「读设计」路径（读 RTLIL vs 读 SV 文件列表），同时给出会自相矛盾、都不给又无设计可读。把这种「恰好一个」的约束用断言固化在构建期，比留到运行期报错更早暴露误用。

**练习 2**：`synth_xilinx` 的 `-json` 输出与 u5-l1 的 `write_rtlil` 输出有何本质区别？

**参考答案**：`write_rtlil` 产出的是**未映射**的通用技术网表（Yosys 内部 cell，如 `$and`/`$dff`）；`synth_xilinx -json` 产出的是**已映射**到 Xilinx 7 系列原语（`LUT6`/`FDRE`/`DSP48E1` 等）的 JSON，nextpnr 与资源报告都消费它。

---

### 4.2 物理实现：`nextpnr-xilinx` 把 JSON 布局布线成 FASM（mkFasm）

#### 4.2.1 概念说明

综合只决定了「用什么单元」，还没决定「单元摆哪、线怎么走」。`mkFasm` 调用开源 PnR 工具 `nextpnr-xilinx`（openXC7 项目），把映射网表 JSON 摆到目标芯片 `xc7k480tffg1156` 的物理位置上并连通布线，输出 FASM。

在 PnR 之前还需要约束——告诉工具「时钟、复位、LED 这些顶层端口对应封装上哪个管脚、什么电平标准」。`mkXdc` 负责把约束文件合并起来。

> 为什么 matmul 能走到这一步、TinyStories 走不到？nextpnr 要在内存里建立整颗芯片的布线图。TinyStories-1M 的设计比 XC7K480T 大约 141 倍（见 u1-l4），nextpnr-xilinx 在这条路上会 OOM。而 matmul 是个 16×16 的整数点积，足够小，能完整跑完 PnR 出 FASM。**matmul 因此成为整条开源工具链「能出比特流」的端到端证明。**

#### 4.2.2 核心流程

1. **准备约束（mkXdc）**：把「板级 XDC」（可选）与「额外约束」`cat` 到一起，产出一份合并 `.xdc`。matmul 自测外壳不需要板级 XDC，只用自己的引脚约束。
2. **检查芯片库**：确认目标 part 的 chipdb 二进制存在。
3. **运行 nextpnr-xilinx**：`--chipdb`（芯片库）+ `--json`（输入网表）+ `--xdc`（约束）→ `--fasm`（输出）。

#### 4.2.3 源码精读

`mkXdc`——合并约束文件：

[flake.nix:629-635](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L629-L635) — `constraintFiles` 由（可选的）`boardXdc` 与 `extraConstraints` 拼成，`cat $constraintFiles > "$out"` 合并成单个 `.xdc`。`boardXdc` 来自 [flake.nix:258](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L258)（一块叫 `ypcb003381p1` 的板子的约束）。

`mkFasm`——nextpnr 调用：

[flake.nix:637-649](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L637-L649) — 先校验 chipdb 存在，再调 `${openXC7Nextpnr}/bin/nextpnr-xilinx --chipdb ... --xdc ... --json ... --fasm "$out"`。四个参数一一对应「芯片库 / 约束 / 输入网表 / 输出 FASM」。

chipdb 与 part 的定义：

[flake.nix:183-187](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L183-L187) — `fpgaPartName = "xc7k480tffg1156-1"`、`fpgaChipdb = ".../xc7k480tffg1156.bin"`。chipdb 是 openXC7 为该 part 预编译的「芯片地图」（编码所有 tile、BEL、布线资源）。

nextpnr 工具本身来自 openXC7 的 Nix 包，并用项目自维护的 fork 源码覆盖构建：

[flake.nix:167-174](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L167-L174) — `openXC7Nextpnr`（用 `nextpnrXilinxFork` 覆盖 src）、`openXC7Chipdb`（kintex7 footprint `xc7k480tffg1156`）、`openXC7Fasm`（FASM 工具集，4.3 用）。

#### 4.2.4 代码实践

**目标**：追踪 matmul 自测外壳的约束来源，理解它为何 `includeBoardXdc = false`。

**步骤**：

1. 读 `matmulSelftestXdc` 的定义：
   [flake.nix:687-691](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L687-L691) — 注意 `includeBoardXdc = false`，`extraConstraints = [ ./fpga/constraints/matmul_selftest.xdc ]`。
2. 打开该约束文件：
   [fpga/constraints/matmul_selftest.xdc:1-12](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/fpga/constraints/matmul_selftest.xdc#L1-L12) — 把 `SYS_CLK`/`SYS_RSTN`/`led_3bits_tri_o[0..2]` 分别绑到 `AA28`/`R28`/`P30`/`M30`/`N30`，电平标准 `LVCMOS18`。
3. 再看 `matmulSelftestFasm`：
   [flake.nix:692-696](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L692-L696) — 它把上一步的 `xdc` 与 4.1 的 `json` 喂给 `mkFasm`。

**观察现象**：nextpnr 日志会报告器件利用率（device utilisation）、布线结果与 FASM 输出路径。

**预期结果**：得到 `/nix/store/...-matmul-selftest.fasm`，一个文本文件，逐行列出要编程的特性。

**待本地验证**：nextpnr 跑通需较大内存与较长时间，本讲未在沙箱内实跑。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `matmulSelftestXdc` 设 `includeBoardXdc = false`？

**参考答案**：自测外壳是为「点亮的 LED + 时钟 + 复位」这套最小引脚设计的，它的约束在 `matmul_selftest.xdc` 里已经自洽；不需要再叠加某块具体开发板的板级约束（那会引入与外壳端口不匹配的引脚定义）。

**练习 2**：若把 `mkFasm` 的 `--chipdb` 指向一个 footprint 不匹配的 part，会发生什么？

**参考答案**：chipdb 编码的是具体 part 的物理资源；若与网表/约束里的 part 不一致，nextpnr 会在加载芯片库或绑定管脚时报错（找不到对应 BEL 或管脚），FASM 无法生成。

---

### 4.3 比特流生成：FASM 经帧数据库转 `.bit`（mkBitstream）

#### 4.3.1 概念说明

FASM 描述了「要点亮哪些特性」，但 FPGA 实际加载的是二进制**比特流**。Xilinx 7 系列的配置存储按「帧（frame）」组织，每帧几十位宽、覆盖整列。把 FASM 翻译成帧、再把帧包成 `.bit`，由 Project X-Ray（prjxray）的两个工具完成：

- `fasm2frames`：FASM → 帧（依赖 prjxray 逆向出的 part 帧数据库）；
- `xc7frames2bit`：帧 + part 元数据 → `.bit`。

`mkBitstream` 把这两步串成一个派生。这是开源三段式 `JSON → FASM → bit` 的最后一段。

#### 4.3.2 核心流程

1. 设好 prjxray 的 Python 路径与帧数据库环境变量（`PRJXRAY_DB_DIR` 等）。
2. `fasm2frames --db-root <part 帧数据库> --part <part 名> <fasm> <输出 .frm>`：把 FASM 展开成帧文件。
3. `xc7frames2bit --part_file <part.yaml> --frm_file <.frm> --output_file <.bit>`：拼装出最终比特流。

#### 4.3.3 源码精读

`mkBitstream` 完整体：

[flake.nix:651-670](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L651-L670) — `nativeBuildInputs` 带 `openXC7Fasm`（含 `fasm2frames`）、`openXC7Prjxray`（含 `xc7frames2bit`）、`prjxrayPythonDeps`（prjxray 的 Python 依赖）。核心两行：

- [flake.nix:662-665](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L662-L665) — `fasm2frames --db-root "${fpgaPrjxrayFamilyDb}" --part ${fpgaPartName} ${fasm} "$frames"`；
- [flake.nix:666-669](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L666-L669) — `xc7frames2bit --part_file "${fpgaPartFile}" --frm_file "$frames" --output_file "$out"`。

数据库与 part 文件的定义：

- `fpgaPrjxrayFamilyDb`（[flake.nix:184-185](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L184-L185)）：`prjxray-db/kintex7`，即 7 系列中 Kintex 家族的帧/位 spec；
- `fpgaPartFile`（[flake.nix:186](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L186)）：`.../xc7k480tffg1156-1/part.yaml`，含该 part 的比特流元数据。

`matmulSelftestBitstream`——把本段链封口：

[flake.nix:697-701](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L697-L701) — 入参 `fasm = matmulSelftestFasm`，`framesBase = "matmul-selftest"`（决定中间 `.frm` 的文件名）。

最后把它暴露成顶层包：

[flake.nix:795](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L795) — `matmul-selftest-bitstream = matmulSelftestBitstream`，所以 `nix build .#matmul-selftest-bitstream` 直接产出 `.bit`。

#### 4.3.4 代码实践

**目标**：从依赖关系上确认「改 FASM 会重算 bit、但不会重算 JSON」。

**步骤**：

1. 在 `flake.nix` 里依次点开 `matmulSelftestBitstream` → `mkBitstream`（入参 `fasm`）→ `matmulSelftestFasm` → `mkFasm`（入参 `json`/`xdc`）→ `matmulSelftestJson`。整条链是 `json → xdc → fasm → bit`。
2. 列出每步工具：JSON 阶段 = Yosys `synth_xilinx`；FASM 阶段 = `nextpnr-xilinx`；bit 阶段 = `fasm2frames` + `xc7frames2bit`。
3. 思考缓存（回顾 u3-l5 的惰性 + 内容寻址）：因为每个 `mk*` 都是独立 `runCommand` 派生，若只改约束 `.xdc`，则 `matmulSelftestJson`（输入哈希没变）命中缓存不重算，`matmulSelftestFasm` 及下游重算。

**预期结果**：能画出四段依赖链并标注工具。

**待本地验证**：实跑 `nix build .#matmul-selftest-bitstream -L` 可观察 nextpnr 与帧生成的日志；本讲未在沙箱内执行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fasm2frames` 需要 `--db-root` 与 `--part` 两个参数？

**参考答案**：FASM 只说「要点亮哪些特性名」，而每个特性对应到芯片里哪些帧、哪些位，是 part 特定的，记录在 prjxray 的帧数据库里。`--db-root` 给家族库，`--part` 限定到具体型号，二者合力才能把 FASM 展开成该 part 的帧数据。

**练习 2**：`matmul-selftest-bitstream` 与 TinyStories 的产物路径终点有何不同？

**参考答案**：matmul 终点是 `.bit`（比特流，可烧板）；TinyStories-1M 终点是「utilization 报告」（`summary.txt/json` + `stat.json`），因为它超配 141 倍、nextpnr 会 OOM，**走不到 FASM 与 bit**。这正是 matmul 作为「端到端可出片」示范的意义。

---

### 4.4 板级自测外壳：`matmul_selftest_top.sv`

#### 4.4.1 概念说明

有了 `.bit`，还要回答一个问题：**烧上板子之后，怎么知道 matmul 算对了？** 板子上没有 Python、没有 `test_vectors.json`，只有 LED。

`matmul_selftest_top.sv` 是一个「自包含」的自测外壳：它包住生成的 `main` DUT，在**综合期**用 SystemVerilog 常量函数重新算出与 PyTorch 黄金参考一致的输入向量与期望值（816），上电后自己播种输入、触发计算、比对结果，并用 LED 报告 pass/fail。

这是本讲最精妙的设计：仿真侧（u4-l1/u4-l2）用 **PyTorch 当黄金参考**；板级侧无法跑 Python，于是用 **SV 常量函数在综合期重算同一个期望值**。两条路殊途同归到 `EXPECTED = 816`，形成交叉验证。

> 与 u4-l2 的关系：仿真 testbench 用层次化引用 `dut.handshake_memory0...` 在仿真特权下播种输入；本讲外壳用 `u_dut.handshake_memory0...` 做**同一件事**，但目的是在真实硅片上电后播种，所以还多了上电复位、超时保护与 LED 状态机。

#### 4.4.2 核心流程

外壳的状态机（按周期推进）：

1. **上电复位**：`boot_count` 计满 `BOOT_RESET_CYCLES`(16) 前，`reset` 持续有效。
2. **播种内部存储**：复位期间，逐拍把 `vec_a(i)`/`vec_b(i)` 写进 DUT 的内部 Handshake 存储 `handshake_memory0/1`（共 16 拍），写完置 `memories_initialized`。
3. **释放复位、触发计算**：`reset` 拉低后，在 start 通道（`in3`）发一个 `valid` 脉冲，等 `ready` 后撤销（与 u4-l2 一致的单脉冲触发）。
4. **捕获结果**：结果从 `in2_st0` 通道出来，比对 `EXPECTED`：
   - 相等 → `pass_latched`；
   - 不等 → `fail_latched`；
   - 若 `cycle_count` 超过 `TIMEOUT_CYCLES`(50,000,000) 仍无结果 → `fail_latched`。
5. **LED 报告**：`led[0]`=心跳闪烁、`led[1]`=pass、`led[2]`=fail。

期望值的综合期计算：

\[
\text{vec\_a}(i)=i+1,\quad \text{vec\_b}(i)=16-i,\qquad
\text{EXPECTED}=\sum_{i=0}^{15}(i+1)(16-i)=816
\]

因为 `vec_a`/`vec_b`/`expected_result` 都是 `function automatic` 且只依赖常量循环，整个 `EXPECTED` 在 elaboration 阶段被常量折叠成 32 位常量 816——综合出的硬件里只有「拿结果和 816 比」的一组比较器，**循环本身不进硬件**。

#### 4.4.3 源码精读

模块端口——只有时钟、复位（低有效）和 3 位 LED：

[matmul_selftest_top.sv:1-5](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/fpga/rtl/matmul_selftest_top.sv#L1-L5)

三个常量函数 + 期望值 localparam——核心在综合期折叠：

[matmul_selftest_top.sv:27-47](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/fpga/rtl/matmul_selftest_top.sv#L27-L47) — `vec_a(idx)={28'd0,idx}+1`、`vec_b(idx)=16-{28'd0,idx}`，`expected_result()` 用 `for` 循环累加 `vec_a(idx)*vec_b(idx)`，最后 `localparam EXPECTED = expected_result()`。注意循环用 `int idx` 且边界为常量 0/16，综合器能把整段折叠成常量 816。

上电复位 + 存储播种（层次化引用，与 u4-l2 同源）：

[matmul_selftest_top.sv:62-78](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/fpga/rtl/matmul_selftest_top.sv#L62-L78) — 复位期逐拍写 `u_dut.handshake_memory0._handshake_memory_5[i] <= vec_a(i)` 与 `handshake_memory1..._4[i] <= vec_b(i)`；`mem_init_idx` 到 15 时置 `memories_initialized`。

复位合成：boot 计数与「存储未就绪」任一为真就保持复位：

[matmul_selftest_top.sv:59](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/fpga/rtl/matmul_selftest_top.sv#L59) — `assign reset = (boot_count <= BOOT_RESET_CYCLES) || !memories_initialized`。

结果捕获 + 超时：

[matmul_selftest_top.sv:90-116](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/fpga/rtl/matmul_selftest_top.sv#L90-L116) — 收到 `in2_st0_valid` 就比 `in2_st0.data == EXPECTED`；否则计 `cycle_count`，超 `TIMEOUT_CYCLES` 即判 fail。一旦 `pass_latched||fail_latched` 就停止再判定（锁存结果）。

LED 映射：

[matmul_selftest_top.sv:118-120](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/fpga/rtl/matmul_selftest_top.sv#L118-L120) — `led[0]=blink_count[25]`（心跳）、`led[1]=pass_latched`、`led[2]=fail_latched`。

DUT 例化——把外壳的逻辑接到生成 `main` 的 ESI 通道：

[matmul_selftest_top.sv:122-134](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/fpga/rtl/matmul_selftest_top.sv#L122-L134) — `main u_dut(...)`，注意 `.clock(SYS_CLK)`、`.reset(reset)`，以及 `in2_st0_done_valid(1'b0)`、各 `ready` 接外壳信号。

#### 4.4.4 代码实践

**目标**：亲手验证「`EXPECTED` 是综合期常量折叠的结果，循环不进硬件」。

**步骤**：

1. 在 [matmul_selftest_top.sv:35-47](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/fpga/rtl/matmul_selftest_top.sv#L35-L47) 的 `expected_result()` 里，把循环上界从 `16` 改成 `8`，手算新的 `EXPECTED`：\(\sum_{i=0}^{7}(i+1)(16-i)\)。
2. 对照 `sim/test_vectors.json`（[sim/test_vectors.json:7-42](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/test_vectors.json#L7-L42)）：`a=[1..16]`、`b=[16..1]`，与 `vec_a`/`vec_b` 一一对应，点积 = 816。
3. 思考：若把 `localparam EXPECTED = expected_result()` 改成在 `always_ff` 里每拍重算，资源占用会有什么变化？

**观察现象**：综合后查看网表，`EXPECTED` 相关逻辑应只表现为「32 位常量 816 与 `in2_st0.data` 的等值比较器」，没有可见的循环硬件。

**预期结果**：手算 \(i=0..7\) 得 \((1·16)+(2·15)+...+(8·9)=16+30+42+52+60+66+70+72=408\)（恰好是 816 的一半，因向量对称）。

**待本地验证**：综合产物中比较器一侧的常量值需在本地用 Yosys 综合后确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么外壳要重新用 SV 函数算 `EXPECTED`，而不是直接 `include` 仿真用的 `tb_data.sv`？

**参考答案**：板级没有 Python 运行时，`tb_data.sv` 的生成依赖 `gen_tb_data.py` 调 PyTorch；而且外壳需要在综合期就把期望值固定成常量、用最少的引脚（只有 LED）报告。用 SV 常量函数让外壳完全自包含，且与 PyTorch 黄金参考（816）交叉验证一致。

**练习 2**：`assign reset = (boot_count <= BOOT_RESET_CYCLES) || !memories_initialized` 里，为什么必须等 `memories_initialized`？

**参考答案**：DUT 的输入被内化为内部 Handshake 存储、未暴露成端口（见 u4-l2）。复位期间逐拍把 `vec_a`/`vec_b` 播种进这些存储；只有播种完成（`memories_initialized=1`）才释放复位，DUT 才能读到正确的输入开始计算。否则 DUT 会在存储还是默认值时就开始算，得到错误结果。

**练习 3**：超时 `TIMEOUT_CYCLES = 50_000_000` 的意义是什么？

**参考答案**：若 DUT 因任何原因（降级错误、握手死锁、存储未就绪）迟迟不在 `in2_st0` 给出结果，外壳不会无限等待，而是超时后点亮 fail LED，给出明确的「没通过」信号，避免板子上看起来「既没 pass 也没 fail」的歧义状态。

---

## 5. 综合实践

把本讲四段串成一条端到端追踪任务（即本讲规格里的核心实践）。

**任务 A：追踪 `matmulSelftestBitstream` 的派生链与每步工具。**

1. 从 [flake.nix:795](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L795) 的 `matmul-selftest-bitstream` 出发。
2. 逆向画依赖链，按下表填空：

| 派生 | 定义行 | 工具 | 输入 | 输出 |
|------|--------|------|------|------|
| `matmulSelftestJson` | [681-686](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L681-L686) | Yosys `synth_xilinx`（slang 前端读 SV） | `matmulSv/sources.f` + 外壳 SV | 映射网表 `.json` |
| `matmulSelftestXdc` | [687-691](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L687-L691) | `cat` 合并 | `matmul_selftest.xdc` | 合并 `.xdc` |
| `matmulSelftestFasm` | [692-696](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L692-L696) | `nextpnr-xilinx` | json + xdc | `.fasm` |
| `matmulSelftestBitstream` | [697-701](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L697-L701) | `fasm2frames` + `xc7frames2bit` | fasm | `.bit` |

3. 写出三段式：`JSON → FASM → bit` 分别对应 `synth_xilinx` / `nextpnr-xilinx` / `fasm2frames+xc7frames2bit`。

**任务 B：解释 `EXPECTED` 如何在综合期由函数算出。**

4. 读 [matmul_selftest_top.sv:35-47](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/fpga/rtl/matmul_selftest_top.sv#L35-L47)：`expected_result()` 是 `function automatic`，内部 `for` 循环边界 0 与 16 均为常量，`vec_a`/`vec_b` 也是常量函数 → 整体在 elaboration 期可求值。
5. `localparam logic [31:0] EXPECTED = expected_result()` 触发该求值，常量折叠为 816，写进网表。
6. 因此综合出的硬件里没有「循环加法器」，只有「`in2_st0.data == 32'd816`」的比较逻辑（见 [matmul_selftest_top.sv:104](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/fpga/rtl/matmul_selftest_top.sv#L104)）。

**预期结果**：能完整复述「四段派生链 + 三段式工具 + EXPECTED 综合期折叠」三件事。

**待本地验证**：可用 `nix build .#matmul-selftest-bitstream -L` 实跑全链，观察 nextpnr 与帧生成日志；若想看 `EXPECTED` 折叠结果，可对 `matmulSelftestJson` 的产物做 `yosys -p "read_json ...; select ...; show"` 等检查。

## 6. 本讲小结

- **开源比特流三段式** `JSON → FASM → bit`：`synth_xilinx`（综合）→ `nextpnr-xilinx`（布局布线）→ `fasm2frames`+`xc7frames2bit`（帧拼装）。
- 四个 `mk*` 函数都是 `runCommand` 派生，串成 `matmulSelftestBitstream`；改某一环只重算它及下游（Nix 内容寻址缓存）。
- `mkSynthJson` 的 JSON 是**分叉点**：既喂 nextpnr 做物理实现，又喂资源报告（u5-l3）做核算。
- **只有 matmul 走得到 `.bit`**：它足够小；141 倍超配的 TinyStories 会在 nextpnr OOM，终点停在 utilization 报告——matmul 因此是开源工具链端到端「能出片」的证明。
- `matmul_selftest_top.sv` 用 SV **常量函数**在综合期算出 `EXPECTED=816`，与 PyTorch 黄金参考交叉验证；上电后自播种、自触发、自比对，用 LED 报告 pass/fail，无需板侧 Python。

## 7. 下一步学习建议

- **u5-l3（资源利用报告）**：看 `write_utilization_report.py` 如何消费本讲的映射 JSON，递归统计 LUT/FF/DSP/BRAM 并与 XC7K480T 容量对比——理解为什么 TinyStories 终点不是 `.bit` 而是这份报告。
- **u5-l4（分阶段 Yosys 综合）**：本讲 `mkSynthJson` 一次性跑完 `synth_xilinx`；u5-l4 会拆成 `mkSynthJsonStages` 的多个 `-run` 区间，逐步观察 coarse/opt/map_cells/map_ffs 等子阶段，并做 targeted memory_map。
- 若对板侧行为感兴趣，可回顾 **u4-l2**（仿真 testbench 的层次化播种与 valid/ready 握手），与本讲外壳对照：同一套 ESI 通道接口，一个在仿真器里、一个在真实硅片上。
