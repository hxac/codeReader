# 其他前端：RTLIL/JSON/BLIF/Liberty 与 SystemVerilog

## 1. 本讲目标

在前几讲里，我们已经把 Verilog 前端（`read_verilog`）从头到尾走了一遍：文本 → 词法 → 语法 → AST → `genRTLIL` → RTLIL。本讲要回答一个紧接着的问题：

> Yosys 还能读哪些格式？它们各自怎么变成 RTLIL？

读完本讲，你应当能够：

- 说清 `read_rtlil`、`read_json`、`read_blif` 三个文本前端各自的解析方式与产物；
- 理解 `read_liberty` 如何把一份 Liberty 工艺库解析成「带功能的 blackbox 模块」；
- 知道 SystemVerilog 的「全面支持」由 `frontends/slang` 这个条件编译的前端（依赖 sv-elab + slang）提供，并能定位它的源码与构建开关；
- 牢记一条贯穿全局的结论：**所有前端最终都产出到同一套 RTLIL**，因此一个 `RTLIL::Design` 可以由混合前端拼装而成。

## 2. 前置知识

本讲假设你已经具备以下认知（来自 u5-l1～u5-l4 与 u2/u3）：

- **RTLIL 内存模型**：`Design → Module → Wire/Cell/Process/Memory`，`Cell` 用 `connections_`（端口名→SigSpec）和 `parameters` 两张表描述（见 u2-l3、u3-l1）。
- **前端注册机制**：继承自 `Pass` 的 `Frontend` 子类，构造时 `Frontend("名字", ...)` 会「一名两表」——既登记进 `pass_register`（当命令），又登记进 `frontend_register`（当前端种类），命令名自动拼成 `read_<名字>`（见 u4-l1）。
- **write_rtlil 文本格式**：行导向、关键字 `module/wire/cell/connect/process` 用 `end` 闭合，标识符以 `\` 或 `$` 开头（见 u2-l1）。
- **blackbox/whitebox**：带 `blackbox` 属性的模块只有端口没有内部实现，用来表示「外部库单元」；`whitebox` 则带有供仿真用的内部实现（见 u2-l2）。

一个关键直觉：前端就是「把某种外部文本/数据结构翻译成 `RTLIL::Module` 上的一堆 `addWire` / `addCell` / `connect` 调用」。不同前端只是输入语法不同，输出动作高度同构——理解了这一点，下面的源码就是反复验证它。

## 3. 本讲源码地图

| 文件 | 作用 | 对应命令 |
|------|------|----------|
| `frontends/rtlil/rtlil_frontend.cc` | 手写递归下降解析器，读 RTLIL 文本 | `read_rtlil` |
| `frontends/json/jsonparse.cc` | 自带 JSON 解析器 + `json_import`，把 JSON 转成 RTLIL | `read_json` |
| `frontends/blif/blifparse.cc` | 行导向解析器，读 BLIF 网表 | `read_blif` |
| `frontends/liberty/liberty.cc` | 读 Liberty 工艺库，每个 cell 变成一个 RTLIL 模块 | `read_liberty` |
| `passes/techmap/libparse.h`（及 `libparse.cc`） | Liberty 的词法/语法解析与 `LibertyAst` 树、缓存 | 被 `read_liberty` 与 `abc9` 复用 |
| `frontends/slang/CMakeLists.txt` | slang（SystemVerilog）前端的构建声明 | `read_slang` |

整体位置关系：这些都是 `frontends/` 下与 `verilog/` 并列的兄弟目录，最终都通过继承 `Frontend` 注入到全局命令表里。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **RTLIL / JSON / BLIF 三个文本前端**——它们处理的都是「网表或近网表」文本；
2. **Liberty 前端**——它处理的不是设计，而是「单元库（工艺库）」，并且要解析布尔表达式；
3. **SystemVerilog（slang）前端现状**——它是条件编译、依赖外部库的新前端。

### 4.1 RTLIL / JSON / BLIF 三个文本前端

#### 4.1.1 概念说明

这三个前端有一个共同点：输入文件本身就接近「网表」，几乎不包含行为级语义（没有 `always`、没有 `generate`），因此解析器不需要像 Verilog 前端那样先建 AST 再 `simplify/genRTLIL`，而是**一边读文本一边直接调用 RTLIL 的构造接口**（`addWire`/`addCell`/`connect`）。

- **RTLIL 文本**（`.il` / `.rtlil`）：Yosys 自己的中间表示文本，`write_rtlil` 的逆操作就是 `read_rtlil`。它最完整，能无损表达 RTLIL 的所有结构（含 `process`、`memory`）。
- **JSON**（`.json`）：Yosys 定义的一种结构化网表交换格式，由 `write_json` 产出。它是「机器友好」的——下游工具（如 nextpnr）直接消费。格式细节见 `help write_json`。
- **BLIF**（`.blif` / `.eblif`）：学术界与多个 FPGA 流程通用的网表格式，基本元素是「LUT + 锁存器/触发器 + 子电路」。

#### 4.1.2 核心流程

三个前端的总体调度相同，都是 `Frontend::execute` 的标准套路：

```
读命令行选项 → extra_args 打开文件流 → 调用一个「parse_xxx(design, stream, ...)」函数
                                          │
                                          └─ 逐行/逐节点：
                                               new RTLIL::Module
                                               module->addWire(...) / addCell(...)
                                               module->connect(...) 或 cell->setPort(...)
                                               design->add(module)
```

差异主要在「parse_xxx 内部如何切词」：

- RTLIL：递归下降，关键字驱动（`module`→`parse_module`、`wire`→`parse_wire`…）。
- JSON：先建一棵 `JsonNode` 树（字典/数组/字符串/数字），再 `json_import` 遍历树建 RTLIL。
- BLIF：以 `.` 开头的指令行（`.model`/`.names`/`.gate`/`.latch`…）逐行处理。

#### 4.1.3 源码精读

**(a) read_rtlil —— 手写递归下降解析器**

`rtlil_frontend.cc` 顶部注释点明了它的身份：一个手写递归下降解析器。

[frontends/rtlil/rtlil_frontend.cc:20-21](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L20-L21) 说明这是「为 RTLIL 文本表示手写的递归下降解析器」。

所有解析逻辑封装在 `RTLILFrontendWorker` 里，它持有四个开关：

[frontends/rtlil/rtlil_frontend.cc:36-39](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L36-L39) 定义了 `-nooverwrite`、`-overwrite`、`-lib`、`-legalize` 四个标志。其中 `-legalize` 很特别：它不报错，而是「确定性地把非法输入改写成合法的」（比如引用不存在的 wire 就哈希映射到一根已存在的 wire），专门给模糊测试（fuzzing）用，用来生成「随机但合法」的 RTLIL。

顶层入口 `parse()` 用关键字分发：

[frontends/rtlil/rtlil_frontend.cc:868-892](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L868-L892) 是主循环，依次尝试 `attribute`/`module`/`autoidx` 三种顶层关键字；读到 `module` 就调用 `parse_module()`。注意它逐字符地用 `try_parse_keyword` / `try_parse_char` 推进一个 `std::string_view line`，并把空白与 `#` 注释一并吃掉，完全不依赖 flex/bison。

`parse_module` 是 RTLIL 文本结构的缩影：

[frontends/rtlil/rtlil_frontend.cc:439-474](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L439-L474) 在模块体内依次识别 `attribute`/`parameter`/`connect`/`wire`/`cell`/`memory`/`process`/`end`——这与 `write_rtlil` 输出的关键字一一对应，证实了「读写严格互逆」。结尾 `current_module->fixup_ports()` 把端口按 `port_id` 排好序；若 `flag_lib` 为真则 `makeblackbox()` 把模块掏空成黑盒。

前端注册部分遵循 u4-l1 讲过的「一名两表」：

[frontends/rtlil/rtlil_frontend.cc:895-896](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L895-L896) `Frontend("rtlil", "read modules from RTLIL file")` 这一行同时诞生了命令 `read_rtlil` 与前端种类 `rtlil`。

**(b) read_json —— 自带 JSON 解析器 + 树遍历导入**

Yosys 没有用第三方 JSON 库，而是自己写了一个极简解析器。核心数据结构 `JsonNode` 用一个 `char type` 区分四种 JSON 节点：

[frontends/json/jsonparse.cc:24-31](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/json/jsonparse.cc#L24-L31) 中 `type` 取值 `S`=String、`N`=Number、`A`=Array、`D`=Dict，分别对应 `data_string`/`data_number`/`data_array`/`data_dict`。构造函数 `JsonNode(std::istream &f)` 直接从流里读一个值——典型的手写递归下降。

前端入口先校验根节点是字典、再遍历 `modules`：

[frontends/json/jsonparse.cc:652-666](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/json/jsonparse.cc#L652-L666) 构造根 `JsonNode`，确认 `type=='D'`，然后对 `modules` 字典里的每个模块调用 `json_import(design, 名字, 节点)`。

真正的「JSON → RTLIL」翻译在 `json_import` 里，它展示了前端如何按 RTLIL 的构造接口逐块搭建模块：

[frontends/json/jsonparse.cc:290-306](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/json/jsonparse.cc#L290-L306) 新建 `RTLIL::Module`、设名字、读 `attributes` 与 `parameter_default_values`，并准备一张 `dict<int, SigBit> signal_bits`——这是 JSON 格式的关键设计：**每一根线网的每一位都被编了一个整数 id**，端口、cell 端口、连线都通过引用这些 id 来表达连接关系。

cell 的导入最能体现「端口=SigSpec」的模型：

[frontends/json/jsonparse.cc:515-563](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/json/jsonparse.cc#L515-L563) 是 `json_import` 中创建 cell 与处理 connections 的代码段：用 `module->addCell(名字, 类型)` 建单元，然后对 `connections` 字典里的每个端口，把 `bits` 数组逐位翻译成 SigSpec——字符串位 `"0"/"1"/"x"/"z"` 直接变成 `State::S0/S1/Sx/Sz` 常量位；数字位则是 `signal_bits` 表里的某根 wire 的某一位（若表中没有就 `addWire(NEW_ID)` 现场建一根）。最后 `cell->setPort(端口名, sig)` 接线——和 u3-l1 讲的 Cell 接口完全一致。

**(c) read_blif —— 行导向、`.names` 变 `$lut`**

BLIF 解析全部集中在 `parse_blif` 函数里：

[frontends/blif/blifparse.cc:87-88](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/blif/blifparse.cc#L87-L88) 是入口签名 `parse_blif(design, f, dff_name, run_clean, sop_mode, wideports)`。它用一个 `while(1)` 主循环，靠 `read_next_line` 读一行（支持行尾 `\` 续行），再按行首字符分流：`#` 是注释，`.` 开头是指令行，其余行则是 `.names` 的「输入输出真值表」。

`.names` 是 BLIF 表达组合逻辑的核心，Yosys 把它翻成内部 `$lut` 单元：

[frontends/blif/blifparse.cc:556-565](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/blif/blifparse.cc#L556-L565) 创建一个 `$lut` cell，参数 `WIDTH` 是输入位数，参数 `LUT` 是一张长度为 \(2^{\text{WIDTH}}\) 的初始全 `Sx` 真值表，端口 `A` 接输入、`Y` 接输出。随后主循环读到形如 `1-1 1` 的行时，就把所有匹配的输入组合在真值表里置成 `S0`/`S1`（带 `-` 通配位会展开到多个表项）。

BLIF 的宽度由一个常量限制：

[frontends/blif/blifparse.cc:24-24](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/blif/blifparse.cc#L24-L24) `lut_input_plane_limit = 12`，即 `.names` 的输入位数不能超过 12（否则真值表会大到 \(2^{12}=4096\) 项），超过则报错；若给了 `-sop` 选项则改用 `$sop`（积之和）单元避开指数爆炸。

锁存器/触发器由 `.latch` 指令处理：

[frontends/blif/blifparse.cc:365-372](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/blif/blifparse.cc#L365-L372) 按边沿类型 `re/fe/ah/al` 分别调用 `addDffGate`/`addDlatchGate`（门级 `$_DFF_`/`$_DLATCH_` 单元），没有时钟的则退化为 `addFfGate`（纯 `$ff`，无时钟触发器）。

#### 4.1.4 代码实践

**实践目标**：体验「同一份设计 → write_json → read_json → write_rtlil」的往返（round-trip），亲手验证三个前端产出同一套 RTLIL。

**操作步骤**（假设你已按 u1-l2 构建出 `./build/yosys`）：

1. 准备一个小设计 `tiny.v`（示例代码，非项目原有文件）：

   ```verilog
   module top(input a, b, c, output y);
     assign y = (a & b) | c;
   endmodule
   ```

2. 在 shell 里执行：

   ```
   read_verilog tiny.v
   write_json tiny.json
   design -reset
   read_json tiny.json
   write_rtlil tiny_recovered.il
   ```

   也可以一行跑：`./build/yosys -p "read_verilog tiny.v; write_json tiny.json" -p "read_json tiny.json; write_rtlil tiny_recovered.il"`。

**需要观察的现象**：

- 打开 `tiny.json`，找到 `modules` → `top` → `cells`，应能看到一个 `$or`/`$and`（或合并后的 `$bwmux` 等）cell，其 `connections` 里每位用数字 id 引用 `netnames`。
- 打开 `tiny_recovered.il`，应能看到与直接 `read_verilog tiny.v; write_rtlil` 几乎一致的 `module/wire/cell/connect` 结构。

**预期结果**：JSON 与 RTLIL 两种文本描述的是同一个 `RTLIL::Design`，逻辑功能一致。若用 `read_verilog tiny.v; write_rtlil direct.il` 再与 `tiny_recovered.il` 做 `diff`，差异通常只在自动命名（`$auto$...`）与单元格合并细节上。

**待本地验证**：上述命令的实际输出文本需在你本地构建后运行确认；不同版本的内部单元（如是否生成 `$bwmux`）可能略有差异。

#### 4.1.5 小练习与答案

**练习 1**：`read_rtlil -lib` 与 `read_rtlil`（不带选项）读入同一个 `.il` 文件，结果有何不同？

> **答案**：`-lib` 会在每个模块结尾调用 `makeblackbox()`（见 `rtlil_frontend.cc:481-482`），把模块内部实现掏空，只保留端口，相当于把它们都当成「只有端口没有实现」的外部单元；不带选项则原样还原内部的所有 wire/cell/process。

**练习 2**：BLIF 的 `.names` 在输入位数为 13 时会怎样？为什么？

> **答案**：会报错 "names' input plane must have fewer than 13 signals"（`blifparse.cc:550-554`）。因为默认走 `$lut`，其真值表长度为 \(2^{\text{WIDTH}}\)，位宽过大时会指数爆炸；若改用 `read_blif -sop`，则改用 `$sop` 积之和表示，可绕过该限制。

**练习 3**：JSON 格式里，一个 cell 的某位端口值为字符串 `"x"` 与值为数字 `5` 分别表示什么？

> **答案**：字符串 `"x"` 表示该位是常量 `State::Sx`（未知值，见 `jsonparse.cc:539-547`）；数字 `5` 表示该位连接到 `signal_bits` 表里 id 为 5 的那根线网的某一位（即一个真实的 wire bit）。

### 4.2 Liberty 前端：单元库到 RTLIL

#### 4.2.1 概念说明

Liberty（`.lib`）是 ASIC 工艺库的事实标准格式。和前三个前端不同，Liberty 文件描述的**不是设计，而是「单元库」**——里面是一堆 `cell`（如 `NAND2_X1`、`DFF_X2`），每个 cell 声明自己的引脚（pin）、方向（input/output）、功能表达式（`function`）、时序、面积等。

Yosys 读 Liberty 的目的是把这些 cell 注册成 `RTLIL::Module`，供后续 `abc9 -liberty`、`dfflibmap -liberty` 做工艺映射时按名字引用。典型用法分两种：

- `read_liberty -lib xxx.lib`：只建空 blackbox（只要端口，给映射用）；
- `read_liberty xxx.lib`（不带 `-lib`）：除了端口，还把 `function` 解析成真实的逻辑门，得到带内部实现的模块（whitebox 风格），可供仿真或 `abc9` 做功能推导。

#### 4.2.2 核心流程

Liberty 解析分两层，分别在两个文件里：

```
passes/techmap/libparse.{h,cc}      LibertyParser：词法+语法 → LibertyAst 树（带缓存）
frontends/liberty/liberty.cc        LibertyFrontend：遍历 ast，把每个 cell 翻成 RTLIL::Module
```

具体步骤：

1. `LibertyParser` 把文本解析成一棵 `LibertyAst` 树（`library → cell → pin/ff/latch/...`），同一文件按文件名缓存（`LibertyAstCache`），避免重复解析。
2. `LibertyFrontend::execute` 遍历 `ast->children`，对每个 `cell`：
   - 新建 `RTLIL::Module`，名字 = cell 名；
   - 遍历 `pin`，按 `direction` 建对应 `Wire` 并标 `port_input/port_output`；
   - （非 `-lib` 模式）遇到 `ff`/`latch`，调用 `create_ff`/`create_latch` 建时序元件；
   - 遇到输出 pin 的 `function`，用 `parse_func_expr` 把布尔表达式翻译成门级逻辑；
   - `fixup_ports()` + `design->add(module)`。

其中最特别的是第 4 步：Liberty 的 `function` 是一段布尔表达式字符串，例如 `"(!A * B + C)"`，需要被解析并翻译成 RTLIL 门。

#### 4.2.3 源码精读

**(a) LibertyAst 树与解析器**

Liberty 的语法是「标识符 (参数列表) { 子节点 }」的嵌套结构，`LibertyAst` 正好对应：

[passes/techmap/libparse.h:36-46](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.h#L36-L46) 定义了 `LibertyAst`：`id` 是节点名（如 `cell`/`pin`），`args` 是参数列表（如 cell 名），`value` 是简单赋值（如 `direction : input` 的 `input`），`children` 是子节点；`find(name)` 在直接子节点里按名查找。这是一棵通用的「属性树」。

解析器还带缓存，这对工艺库很关键——同一个 `.lib` 可能在一次综合里被 `read_liberty`、`abc9`、`dfflibmap` 反复读取：

[passes/techmap/libparse.h:146-159](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/libparse.h#L146-L159) `LibertyAstCache` 用文件名做 key 缓存解析出的 `shared_ptr<const LibertyAst>`，是个单例（`instance`）。

**(b) 布尔表达式解析：算符优先 + token 栈**

Liberty 的 `function` 表达式用一套类似 C 的算符（`!`/`'` 非、`&`/`*` 与、`|`/`+` 或、`^` 异或）。`parse_func_expr` 用「算符优先 + 栈归约」的方式边读边算，直接生成 RTLIL 门，而不先建 AST：

[frontends/liberty/liberty.cc:145-165](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/liberty/liberty.cc#L145-L165) 是主循环：每读到一个 token（运算符或标识符），就反复调用 `parse_func_reduce` 尝试归约栈顶，再把新 token 压栈。

[frontends/liberty/liberty.cc:57-115](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/liberty/liberty.cc#L57-L115) 是归约规则的核心片段。token 用 `type` 标记优先级层级（`0`→操作数、`1`→异或层、`2`→与层、`3`→或层），配合运算符字符（`'`/`!`/`^`/`&`/`*`/`+`/`|`）。每条规则把栈顶若干 token 合并成一个新 token，并调用 `module->NotGate`/`XorGate`/`AndGate` 等工厂函数生成对应的 RTLIL 门——这正是 u3-l1 讲过的 Module 构造接口的真实应用。

> 这套机制与 u3-l2 的 `SigMap`、u6-l3 的 `opt_expr` 同源：都是在 SigSpec 层面做布尔化简/翻译，只是 Liberty 前端是「从字符串表达式生成门」。

**(c) cell → module 的总装**

前端入口与 cell 遍历：

[frontends/liberty/liberty.cc:481-482](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/liberty/liberty.cc#L481-L482) 注册 `Frontend("liberty", "read cells from liberty file")`，命令即 `read_liberty`。

[frontends/liberty/liberty.cc:598-610](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/liberty/liberty.cc#L598-L610) 先 `LibertyParser parser(*f, filename)` 解析（自动走缓存），再对 `parser.ast->children` 里每个 `id=="cell"` 的节点新建一个 `RTLIL::Module`，名字取自 `cell->args.at(0)`。

`-lib` 与 `-wb` 两种「盒子」属性在这里设置：

[frontends/liberty/liberty.cc:612-616](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/liberty/liberty.cc#L612-L616) 若 `flag_lib` 则 `set_bool_attribute(ID::blackbox)`（黑盒，无实现）；若 `flag_wb` 则设 `whitebox`（白盒，带实现）。注意 `-lib` 与 `-wb` 互斥（`liberty.cc:587-588` 报错）。

每个 pin 变成一根带方向的 wire：

[frontends/liberty/liberty.cc:629-643](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/liberty/liberty.cc#L629-L643) 对 `pin` 子节点查 `direction`，必须是 `input/output/inout/internal` 之一，否则按 `-ignore_miss_dir` 决定报错或跳过；然后 `module->addWire(引脚名)` 建线。

简单组合 cell 还会被标成 `abc9_box`：

[frontends/liberty/liberty.cc:786-787](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/liberty/liberty.cc#L786-L787) 若该 cell 是「纯组合且有输出」（`simple_comb_cell && has_outputs`），就打上 `abc9_box` 属性，提示后续 `abc9` 可把它当作一个可提取的盒子来优化——这是 Liberty 前端与 u6-l6 的 abc9 之间的衔接点。

#### 4.2.4 代码实践

**实践目标**：阅读 `liberty.cc`，确认「单元库如何被解析为 RTLIL blackbox」；并动手用一份现成 liberty 跑通 `read_liberty`。

**操作步骤**：

1. 项目自带一个 liberty 样例 `examples/cmos/cmos_cells.lib`（u1-l4 已用过）。运行：

   ```
   ./build/yosys -p "read_liberty -lib examples/cmos/cmos_cells.lib; ls; write_rtlil cells.il"
   ```

2. 打开 `cells.il`，挑选其中一个 cell（如 PMOS/NAND），观察它是否只有端口、没有内部 cell（blackbox 特征）。

3. 作为对照（源码阅读型实践）：在 `frontends/liberty/liberty.cc:612-616` 旁加一句心理标注——若把命令换成 `read_liberty examples/cmos/cmos_cells.lib`（去掉 `-lib`），代码会进入 `create_ff`/`create_latch`/`parse_func_expr` 分支，生成带内部逻辑的模块。

**需要观察的现象**：

- `ls` 输出里应能看到刚读入的若干以 cell 名命名的模块，且带 `blackbox` 属性。
- `write_rtlil` 输出里这些模块只有 `wire ... input/output` 与 `attribute \blackbox`，没有 `cell` 行。

**预期结果**：`-lib` 模式下，每个 liberty cell 都成为一个只有端口的 blackbox 模块，可供 `dfflibmap`/`abc9 -liberty` 在工艺映射阶段按名字引用。

**待本地验证**：`cmos_cells.lib` 的具体 cell 名与数量需本地运行确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `read_liberty` 要对解析结果按文件名缓存（`LibertyAstCache`）？

> **答案**：一次完整综合里，同一份 `.lib` 经常被 `read_liberty`、`abc9 -liberty`、`dfflibmap -liberty` 多处读取（`libparse.h:146-159`）。缓存避免重复做词法/语法解析，且通过 `shared_ptr<const LibertyAst>` 让多处共享同一棵不可变 AST，省内存。

**练习 2**：`parse_func_expr` 用的是「先建 AST 再翻译」还是「边读边生成门」？

> **答案**：后者。它用算符优先 + token 栈（`liberty.cc:145-165`、`57-115`），每完成一次归约就直接调用 `module->AndGate`/`NotGate` 等生成 RTLIL 门，没有中间的布尔表达式 AST（注意区分：Liberty 文本本身有一棵 `LibertyAst`，但 `function` 字符串内部没有再建 AST）。

**练习 3**：`read_liberty -lib` 与 `read_liberty -wb`（不带 `-lib`）产出的模块，对 `abc9` 有何不同意义？

> **答案**：`-lib` 产出 blackbox（无内部实现），`abc9` 只能把它们当「外部硬宏」按名字保留/替换；不带 `-lib` 时（配合 `-wb` 或默认），组合 cell 还会被标 `abc9_box`（`liberty.cc:786-787`），`abc9` 可以读出其逻辑功能做布尔优化。所以是否带 `-lib` 直接影响 abc9 能否「看穿」单元。

### 4.3 SystemVerilog（slang）前端现状

#### 4.3.1 概念说明

`read_verilog` 只支持「可综合的 Verilog 子集 + 少量 SystemVerilog 特性」（`assert`/`assume`、`$past`、packages、interfaces 等有限支持，详见 `docs/source/using_yosys/verilog.rst`）。要获得「全面的 SystemVerilog 支持」（IEEE 1800-2017/2023），Yosys 引入了第三个 Verilog 家族前端——slang 前端。

它的历史值得一提：早期全面 SV 支持是一个**外部插件** `yosys-slang`（见 `docs/source/using_yosys/more_scripting/load_design.rst:138-146`）；如今它正被**集成进主仓库**，成为 `frontends/slang/` 下条件编译的内置前端。本仓库的 README 第 8 行明确写道：

> Yosys is using [sv-elab](https://github.com/povik/sv-elab) and [slang](https://github.com/MikePopoloski/slang) libraries to provide comprehensive SystemVerilog support.

也就是说，这个前端依赖两个外部库：**slang**（一个独立的开源 SystemVerilog 编译器前端，负责词法/语法/语义分析）与 **sv-elab**（把 slang 的 elaboration 结果桥接到 Yosys RTLIL 的胶水层）。

#### 4.3.2 核心流程

slang 前端的总体形状与 Verilog 前端一致（读 SV → 某种中间表示 → RTLIL），只是「词法/语法/语义分析」整段外包给了 slang 库，sv-elab 负责把 slang 的 AST 翻译成 RTLIL：

```
SystemVerilog 源码
     │  slang 库（词法/语法/语义/elaboration）
     ▼
slang 的 AST / elaboration 设计
     │  sv-elab（frontends/slang/lib/src/*.cc）桥接
     ▼
RTLIL::Design（与其他前端同构）
```

它的命令名是 `read_slang`（文档 `docs/source/using_yosys/more_scripting/load_design.rst:139-140` 里 `help read_slang` 可证）。

#### 4.3.3 源码精读（构建侧）

**重要说明**：sv-elab 是一个 git submodule，路径为 `frontends/slang/lib`（见 `.gitmodules` 中 `[submodule "sv-elab"] path = frontends/slang/lib`）。在当前检出中该目录为空（submodule 未拉取），因此 `frontends/slang/lib/src/slang_frontend.cc` 等真正的翻译代码**不在当前源码树中**，下面对其内部行为的描述属于「待确认」。

我们能确认的是构建侧的声明：

[frontends/slang/CMakeLists.txt:6-8](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/slang/CMakeLists.txt#L6-L8) 用 `yosys_frontend(slang ...)` 宏声明这个前端，源文件列表是 `lib/src/slang_frontend.cc`、`lib/src/builder.cc`、`lib/src/cases.cc`、`lib/src/variables.cc` 等——从文件名能猜出 sv-elab 按「语句/表达式/Case/变量/存储器/过程块」分模块把 slang AST 翻成 RTLIL，与 Verilog 前端的 `genrtlil.cc` 角色相似。

[frontends/slang/CMakeLists.txt:32-37](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/slang/CMakeLists.txt#L32-L37) 关键的条件：`ENABLE_IF YOSYS_ENABLE_SLANG`，并链接 `$<...:slang::slang>`。也就是说，只有当 slang 依赖被探测到（且未设 `YOSYS_WITHOUT_SLANG`）时，这个前端才会被编入。

它在父目录里也是条件包含：

[frontends/CMakeLists.txt:9-11](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/CMakeLists.txt#L9-L11) `if (NOT YOSYS_WITHOUT_SLANG)` 才 `add_subdirectory(slang)`。这与 u1-l2 讲的 `YOSYS_WITH_*`/`YOSYS_ENABLE_*` 条件编译机制完全一致。

构建前置：因为依赖 slang 与 sv-elab 两个 submodule，初次构建前必须：

```
git submodule update --init --recursive
```

（`docs/source/getting_started/installation.rst` 第 53、61 行说明 ABC 等库以 submodule 形式提供，需递归初始化。）

> **待确认**：sv-elab 内部（`lib/src/slang_frontend.cc` 等）如何把 slang 的 elaboration 结果翻译成 RTLIL、`read_slang` 支持哪些命令行选项、其与 `read_verilog` 在行为上的具体差异，需在拉取 submodule 后阅读源码确认。本仓库当前未包含这些源文件。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：在不一定编译 slang 的情况下，仅通过构建文件与文档，确认 slang 前端「是条件编译的内置前端、依赖两个外部库」。

**操作步骤**：

1. 打开 `.gitmodules`，找到 `[submodule "slang"]`（`path = libs/slang`）与 `[submodule "sv-elab"]`（`path = frontends/slang/lib`）两个条目，确认它们分别对应 slang 库与 sv-elab 胶水层。
2. 对照 `frontends/slang/CMakeLists.txt:6-37`，列出 slang 前端依赖的 sv-elab 源文件清单（如 `slang_frontend.cc`/`builder.cc`/`cases.cc`…），猜测各自职责。
3. 查阅 `docs/source/using_yosys/more_scripting/load_design.rst:135-146`，对比「内置 `read_verilog`（有限 SV 支持）」与「`read_slang`（全面 SV 支持）」的定位差异。

**需要观察的现象**：

- `frontends/slang/lib/` 在未拉取 submodule 时为空目录；`libs/slang/` 同理。
- 文档明确 `read_slang` 是一个 `Frontend`，享有内置前端的所有待遇（能被脚本直接调用、能进 `read_*` 自动分发）。

**预期结果**：你能说清「slang 前端 = sv-elab（桥接）+ slang（SV 编译器）两个 submodule，经 `YOSYS_ENABLE_SLANG` 条件编入，命令为 `read_slang`」。

**待本地验证**：若你执行了 `git submodule update --init --recursive` 并以 slang 支持重新构建，可进一步 `./build/yosys -p "help read_slang"` 查看其真实选项；当前环境未编入时该命令不可用。

#### 4.3.5 小练习与答案

**练习 1**：slang 前端与经典 Verilog 前端在「词法/语法分析」阶段的根本区别是什么？

> **答案**：Verilog 前端用 flex（`verilog_lexer.l`）+ bison（`verilog_parser.y`）自己做词法/语法分析（见 u5-l1）；slang 前端则把整段「词法/语法/语义/elaboration」外包给独立的 slang 库（`libs/slang`），Yosys 侧的 sv-elab 只负责把 slang 的结果翻译成 RTLIL。

**练习 2**：为什么 `frontends/slang/lib/src/` 在当前源码树里看不到源文件？

> **答案**：因为 `frontends/slang/lib` 是 sv-elab 这个 git submodule 的挂载点（`.gitmodules`），当前检出没有执行 `git submodule update --init`，故目录为空；真正的 `slang_frontend.cc` 等文件位于 sv-elab 仓库中。

**练习 3**：如果用户构建时显式 `-DYOSYS_WITHOUT_SLANG=ON`，会发生什么？

> **答案**：`frontends/CMakeLists.txt:9-11` 的 `if (NOT YOSYS_WITHOUT_SLANG)` 不成立，`slang` 子目录不会被加入构建，`read_slang` 命令不会存在；用户只能用 `read_verilog` 处理 SystemVerilog（能力受限）。这正是 u1-l2 条件编译机制的一个具体应用。

## 5. 综合实践

把本讲三个模块串起来，完成一次「混合前端」的综合小任务，体会「所有前端都产出同一套 RTLIL」。

**任务**：用 Verilog 写一个含组合逻辑与一个 D 触发器的小设计，分别用三种文本前端做往返，最后用 Liberty 库做一次工艺映射。

**步骤**（示例代码非项目原有文件）：

1. 准备 `top.v`：

   ```verilog
   module top(input clk, input a, b, output reg q);
     always @(posedge clk) q <= (a & b) | q;
   endmodule
   ```

2. 用 JSON 往返：

   ```
   read_verilog top.v
   proc; opt
   write_json top.json
   design -reset
   read_json top.json
   write_rtlil top.il
   ```

3. 用 RTLIL 文本再往返一次（验证 `read_rtlil` 能读回 `write_rtlil` 的输出）：

   ```
   design -reset
   read_rtlil top.il
   ```

4. 用 Liberty 做工艺映射（复用 `examples/cmos/cmos_cells.lib`）：

   ```
   read_liberty -lib examples/cmos/cmos_cells.lib
   abc -liberty examples/cmos/cmos_cells.lib
   stat
   ```

**需要观察与记录的现象**：

- 第 2 步后，`top.il` 里的组合逻辑应已被 `opt` 简化成少量 `$and`/`$or`/`$dff`。
- 第 3 步 `read_rtlil` 能无错读回，说明 RTLIL 文本是自洽的可往返格式。
- 第 4 步 `stat` 前后对比：组合单元是否从 `$and`/`$or` 变成了 liberty 库里的具名单元（如 NAND/NOR 等，取决于 `cmos_cells.lib` 内容）。

**预期结果**：你将直观看到「Verilog → RTLIL → JSON → RTLIL（文本）→ liberty 映射」这条链路里，RTLIL 始终是中间的「标准件」，各前端只是它不同的「衣裳」。

**待本地验证**：`cmos_cells.lib` 中实际可用的单元名、`abc` 映射后的具体单元，需本地运行确认。

## 6. 本讲小结

- Yosys 的文本前端（`read_rtlil`/`read_json`/`read_blif`）处理的都是「近网表」文本，解析器**直接调用 RTLIL 构造接口**（`addWire`/`addCell`/`connect`），不像 Verilog 前端要先建 AST。
- `read_rtlil` 是与 `write_rtlil` 严格互逆的手写递归下降解析器，支持 `-lib`（黑盒）、`-legalize`（模糊测试用）等选项。
- `read_json` 自带一个极简 JSON 解析器（`JsonNode` 的 S/N/A/D 四类节点），再由 `json_import` 遍历树建 RTLIL，用「按位整数 id」表达连接。
- `read_blif` 行导向解析，把 `.names` 翻成 `$lut`（或 `-sop` 下的 `$sop`），`.latch` 翻成 DFF/锁存器门。
- `read_liberty` 解析的是**单元库**而非设计：`LibertyParser` 产出带缓存的 `LibertyAst` 树，每个 `cell` 变成一个 `RTLIL::Module`，`function` 表达式经算符优先的 `parse_func_expr` 翻译成门；`-lib` 产出 blackbox，纯组合 cell 还会被标 `abc9_box`。
- SystemVerilog 的全面支持由条件编译的 slang 前端（`read_slang`）提供，依赖 slang + sv-elab 两个 submodule，经 `YOSYS_ENABLE_SLANG` 编入；其内部翻译细节需拉取 submodule 后确认。
- **贯穿结论**：所有前端最终都产出到同一套 RTLIL，因此一个设计可由多种前端拼装，后端与 pass 对此无感知。

## 7. 下一步学习建议

- **进入后端**：本讲讲完「读」，下一单元（u7）讲「写」——`write_verilog`/`write_rtlil`/`write_json` 是本讲几个前端的镜像，对照阅读能加深对 RTLIL 文本与 JSON 格式的理解。
- **回到核心综合流程**：带着本讲的 Liberty 知识读 u6-l6（`abc9`/`dfflibmap`），你会明白 `-liberty` 选项消费的正是本讲 `read_liberty` 产出的 blackbox 模块。
- **深入 Verilog 前端的对照**：若你对「解析器如何翻译表达式」感兴趣，可回头比较 `liberty.cc` 的 `parse_func_expr`（算符优先+栈）与 `frontends/ast/genrtlil.cc`（AST 大 switch）两种风格的取舍。
- **追踪 slang 进展**：若你实际需要 SystemVerilog，建议拉取 submodule 后阅读 `frontends/slang/lib/src/slang_frontend.cc`，并对照 `docs/source/using_yosys/verilog.rst` 了解 `read_verilog` 已覆盖哪些 SV 特性，判断是否真的需要切到 `read_slang`。
