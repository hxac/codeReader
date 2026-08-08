# 完整工具链走一遍：DSLX → IR → 优化 → Verilog

## 1. 本讲目标

前几讲我们认识了 XLS 的定位（u1-l1）、构建方式（u1-l2）、目录布局（u1-l3），并亲手写出了第一个 DSLX 函数 `gcd.x`（u1-l4）。本讲把这一切串起来：**沿着官方 Quick Start，亲手把一个 `.x` 文件一路跑到 Verilog**。

学完本讲，你应当能够：

1. 说出 XLS 端到端命令行工作流的四步命令，以及每一步的输入/输出文件（`.x → .ir → .opt.ir → .v`）。
2. 解释 `interpreter_main`、`ir_converter_main`、`opt_main`、`codegen_main` 四个工具各自的作用与定位。
3. 用 `diff` 观察优化前后 IR 的差异，理解优化器消除了什么。
4. 读懂 `codegen_main` 生成的 Verilog 模块的基本结构（时钟、端口、寄存器）。
5. 对应到真实源码：知道每个命令背后调用的是哪个库函数。

---

## 2. 前置知识

本讲默认你已经掌握前四讲的内容，尤其是：

- **DSLX 函数与测试语法**（u1-l4）：`fn`、`#[test]`、`assert_eq` 的写法，以及"加 `u32:0`"这种恒等冗余表达式。
- **XLS IR 是真相之锚**（u1-l1、u1-l3）：前端（DSLX / xlscc）最终都汇入唯一的 XLS IR，后续优化、调度、代码生成都在 IR 之上进行。

补充三个本讲要用到的小概念：

| 术语 | 一句话解释 |
| --- | --- |
| **顶层实体（top）** | 一个 Package 里作为入口的函数或 Proc。codegen 时必须指定顶层。 |
| **优化等级（opt level）** | 控制优化管线激进程度的档位，`opt_main` 默认取最大档 `kMaxOptLevel`。 |
| **延迟模型（delay model）** | 描述每种运算在目标工艺下耗时的模型，是流水线调度的输入。`unit` 是最简单的"每个运算耗时 1 单位"模型。 |

本讲涉及的命令都以"已经 `bazel build -c opt //xls/...` 完成、可直接用 `./bazel-bin/...` 下的二进制"为前提（见 u1-l2）。如果你更习惯 `bazel run`，把 `./bazel-bin/xls/...` 换成 `bazel run -c opt //xls/...` 即可。

---

## 3. 本讲源码地图

本讲围绕"一条命令链"展开，关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `docs_src/tools_quick_start.md` | 官方快速上手文档，本讲的命令蓝本，包含 `simple_add.x` 示例。 |
| `xls/dslx/ir_convert/ir_converter_main.cc` | DSLX → IR 转换工具的命令行入口。 |
| `xls/dslx/ir_convert/ir_converter.h` | 上述工具背后的库函数 `ConvertFilesToPackage` 声明。 |
| `xls/dslx/ir_convert/conversion_info.h` | 转换产物 `PackageConversionData` 的定义（含 `DumpIr()`）。 |
| `xls/tools/opt_main.cc` | IR 优化工具的命令行入口。 |
| `xls/tools/opt.h` | 优化库函数 `OptimizeIrForTop` 与 `OptOptions` 声明。 |
| `xls/tools/codegen_main.cc` | Verilog 代码生成工具的命令行入口。 |
| `xls/tools/codegen.h` | 代码生成库函数 `Schedule` / `Codegen` 声明。 |
| `xls/tools/codegen_flags.cc` / `scheduling_options_flags.cc` | codegen 与调度相关命令行标志的定义（`--generator`、`--pipeline_stages`、`--delay_model`）。 |

一句话：三个转换工具（`ir_converter_main`、`opt_main`、`codegen_main`）都是"薄壳"，真正干活的是它们各自 `include` 的库函数；`interpreter_main` 则是另一类——它不产生中间文件，而是直接运行测试。

---

## 4. 核心概念与源码讲解

本讲按"先总览，再逐工具精读"的顺序，拆成四个最小模块：工具链总览、DSLX→IR、IR 优化、IR→Verilog。

### 4.1 工具链总览：四步命令与阶段产物

#### 4.1.1 概念说明

XLS 的命令行工作流是一条**单向流水线**：上一步的输出文件就是下一步的输入文件，每一步都用一个独立的二进制工具。这四步分别是：

1. **`interpreter_main`** —— 直接解释执行 `.x`，用来跑 `#[test]` / `#[quickcheck]`，做交互式开发与调试。它**不产生 IR/Verilog**，只是验证你的 DSLX 逻辑正确。
2. **`ir_converter_main`** —— 把类型检查通过的 DSLX 翻译成 XLS IR 文本（`.ir`）。
3. **`opt_main`** —— 在 IR 上跑标准优化管线，输出更精简的 `.opt.ir`。
4. **`codegen_main`** —— 对优化后的 IR 做流水线调度并生成 Verilog（`.v`）。

数据流可以画成：

```
simple_add.x ──(interpreter_main)──► 终端打印测试结果（不落盘）
            │
            └─(ir_converter_main)──► simple_add.ir
                                          │
                            (opt_main)────┴──► simple_add.opt.ir
                                                          │
                                            (codegen_main)┴──► simple_add.v
```

要点：`interpreter_main` 与后面三步是**并行可选**的关系——你可以先用它确认 DSLX 正确，再进入"转换—优化—生成"这条会落盘的链路。理解这一分工，就不会把"跑测试"和"生成硬件"混淆。

#### 4.1.2 核心流程

官方 Quick Start 给出的最小示例文件 `simple_add.x` 长这样（注释里特意留了一个"可被优化掉"的 `+ u32:0`）：

```
fn add(x: u32, y: u32) -> u32 {
  x + y + u32:0  // Something to optimize.
}

#[test]
fn test_add() {
  assert_eq(add(u32:2, u32:3), u32:5)
}
```

对应的四步命令（以 `./bazel-bin` 下已编译好的二进制为例）：

```
# 第 0 步（可选）：解释执行，跑测试
./bazel-bin/xls/dslx/interpreter_main /tmp/simple_add.x

# 第 1 步：DSLX → IR
./bazel-bin/xls/dslx/ir_convert/ir_converter_main --top=add /tmp/simple_add.x > /tmp/simple_add.ir

# 第 2 步：IR 优化
./bazel-bin/xls/tools/opt_main /tmp/simple_add.ir > /tmp/simple_add.opt.ir

# 第 3 步：IR → Verilog（1 级流水线，unit 延迟模型）
./bazel-bin/xls/tools/codegen_main --pipeline_stages=1 --delay_model=unit /tmp/simple_add.opt.ir > /tmp/simple_add.v
```

各阶段产物对照：

| 阶段 | 输入 | 工具 | 输出 | 产物形态 |
| --- | --- | --- | --- | --- |
| 解释 | `.x` | `interpreter_main` | 终端 `[ RUN ]/[ OK ]` | 不落盘 |
| 转换 | `.x` | `ir_converter_main` | `.ir` | 人类可读 IR 文本 |
| 优化 | `.ir` | `opt_main` | `.opt.ir` | 更精简的 IR 文本 |
| 生成 | `.opt.ir` | `codegen_main` | `.v` | SystemVerilog 文本 |

#### 4.1.3 源码精读

这四条命令的权威出处就是官方文档本身。Quick Start 把示例文件、四步命令、以及"用 diff 看优化效果"的建议都写在一起：

[docs_src/tools_quick_start.md:11-22](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tools_quick_start.md#L11-L22) —— `simple_add.x` 的完整内容（函数 + 单元测试）。

[docs_src/tools_quick_start.md:49-55](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tools_quick_start.md#L49-L55) —— 第 1 步：`ir_converter_main --top=add` 把 DSLX 转成 IR。

[docs_src/tools_quick_start.md:57-66](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tools_quick_start.md#L57-L66) —— 第 2 步：`opt_main` 优化，并提示用 `diff -U8 /tmp/simple_add*.ir` 观察优化器如何"消除无用的 add-with-zero"。

[docs_src/tools_quick_start.md:68-74](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tools_quick_start.md#L68-L74) —— 第 3 步：`codegen_main --pipeline_stages=1 --delay_model=unit` 生成 Verilog。

> 小贴士：Quick Start 还附带一个 IR 可视化工具 `ir_viz`（[tools_quick_start.md:78-85](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tools_quick_start.md#L78-L85)），可在浏览器里看 IR 的数据流图，本讲后续不展开。

#### 4.1.4 代码实践

1. **实践目标**：把四步命令亲手敲一遍，建立"输入→输出"的肌肉记忆。
2. **操作步骤**：用上面的命令，从创建 `/tmp/simple_add.x` 开始，依次执行解释、转换、优化、生成四步。
3. **需要观察的现象**：每一步执行后，`/tmp/` 下是否多出对应文件（`.ir`、`.opt.ir`、`.v`）。
4. **预期结果**：`ls /tmp/simple_add*` 应能看到 `.x`、`.ir`、`.opt.ir`、`.v` 四个文件。
5. **结果**：精确的终端输出与文件内容**待本地验证**（取决于你的 XLS 版本与默认选项）。

#### 4.1.5 小练习与答案

**练习 1**：如果跳过第 2 步（优化），直接对 `simple_add.ir` 跑 codegen，会发生什么？算出来的硬件功能还正确吗？

> **参考答案**：仍然正确。优化只是让 IR 更精简（少几个节点、少几个寄存器），不改变功能语义。跳过优化会让生成的 Verilog 里保留冗余的 `+0` 运算，但 `x + 0 == x`，功能不变，只是面积/延迟更差。

**练习 2**：为什么 `ir_converter_main` 需要传 `--top=add`，而 `opt_main` 和 `codegen_main` 不需要？

> **参考答案**：转换阶段可能一个 `.x` 里有多个函数，需要指明哪个作为入口（顶层）来决定转换范围与 Package 名；而后续步骤直接吃一个已经成型的 IR 文件，Package 内通常已经标好了顶层（codegen 会在没有顶层时按 `--top` 设置，见 4.4.3）。

---

### 4.2 ir_converter_main：把 DSLX 转换成 IR

#### 4.2.1 概念说明

`ir_converter_main` 是 DSLX 前端"落地"到 IR 的出口。它接收一个或多个 `.x` 文件，先做解析与类型检查（见 u2 单元），再把类型检查后的 AST 翻译成 XLS IR 文本，打印到 stdout（或用 `--output_file` 写盘）。

它解决的问题是：**DSLX 是给人写的高层语言，IR 是给优化器和代码生成器用的低层中间表示**——两者之间需要一次确定性的"lowering（降级）"翻译。注意它和 `interpreter_main` 的区别：解释器是"运行"，转换器是"翻译"，二者都基于同一套类型检查，但产物完全不同。

#### 4.2.2 核心流程

`ir_converter_main` 的执行流程可以概括为：

```
命令行参数 (.x 路径, --top, 各种 ConvertOptions)
        │
        ▼
GetIrConverterOptionsFlagsProto()   # 把 ABSL flags 收集成 proto
        │
        ▼
ConvertFilesToPackage(...)          # 核心：解析+类型检查+翻译
        │  返回 PackageConversionData { package, interface }
        ▼
result.DumpIr()  →  stdout / --output_file
```

其中 `ConvertFilesToPackage` 是真正的核心：它把 DSLX 文件读进来、解析成 AST、做类型推导，再把每个函数/Proc翻译成 IR 的 `Function`/`Proc`，组装进一个 `Package`。翻译细节（如 `if`/`match` 如何变成 `kSelect`）会在 u3-l4 专门讲，本讲只需知道"它产出一个 `Package`，再 `DumpIr()` 成文本"。

#### 4.2.3 源码精读

命令行入口在 `ir_converter_main.cc` 的 `RealMain` 里，核心就一行真正干活的调用：

[xls/dslx/ir_convert/ir_converter_main.cc:62](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter_main.cc#L62) —— `RealMain` 的起点，先把一堆命令行标志（`--top`、`--dslx_stdlib_path`、`--convert_tests`、警告开关等）读进 `IrConverterOptionsFlagsProto` 与 `ConvertOptions`。

[xls/dslx/ir_convert/ir_converter_main.cc:155-159](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter_main.cc#L155-L159) —— 真正的翻译调用 `ConvertFilesToPackage(...)`，返回 `PackageConversionData result`。注意它把 `top`、`package_name`、`printed_error` 都透传进去。

[xls/dslx/ir_convert/ir_converter_main.cc:160-164](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter_main.cc#L160-L164) —— 拿到 `result` 后，要么写文件、要么 `std::cout << result.package->DumpIr()`，把内存里的 `Package` 序列化成 IR 文本输出。

那个核心库函数的声明（注释里写得很直白："ir_converter_main should be a thin wrapper around this"）：

[xls/dslx/ir_convert/ir_converter.h:113-119](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter.h#L113-L119) —— `ConvertFilesToPackage` 的声明，参数包括 DSLX 文件路径、标准库路径、转换选项、可选顶层名、可选包名。

转换产物 `PackageConversionData` 的结构非常简单：

[xls/dslx/ir_convert/conversion_info.h:27-34](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/conversion_info.h#L27-L34) —— 它只持有两样东西：`std::unique_ptr<Package> package`（IR 包）和 `PackageInterfaceProto interface`（对外接口信息），并提供 `DumpIr()` 便捷方法。

> 旁注：`main` 函数里把 `"-"` 当成 `/dev/stdin` 的简写（[ir_converter_main.cc:196-202](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter_main.cc#L196-L202)），所以你也可以从管道喂 DSLX 进来。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到 `simple_add.x` 被翻译成的 IR 长什么样。
2. **操作步骤**：执行 `./bazel-bin/xls/dslx/ir_convert/ir_converter_main --top=add /tmp/simple_add.x`，直接看终端输出（或重定向到 `/tmp/simple_add.ir`）。
3. **需要观察的现象**：输出里应该有 `package simple_add`、一个 `fn add(...)`，函数体里有 `literal(value=0)` 节点（对应 `u32:0`）和两个 `add` 节点（一个算 `x + y`，一个把结果 `+ 0`）。
4. **预期结果**：你会看到形如下面的结构（**示意，节点名/编号待本地验证**）：
   ```
   package simple_add

   fn add(x: bits[32], y: bits[32]) -> bits[32] {
     literal.3: bits[32] = literal(value=0)
     add.1: bits[32] = add(x, y)
     add.2: bits[32] = add(add.1, literal.3)
     ret add.2: bits[32]
   }
   ```
   关键是：**转换阶段保留了 `+ 0` 这个冗余运算**，它要等到下一步优化才被消除。
5. **结果**：精确节点编号与顺序**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把示例里的 `#[test]` 函数也想要进 IR，该怎么做？

> **参考答案**：默认情况下 `ir_converter_main` **不**转换测试函数。需要加 `--convert_tests` 标志（对应 `ConvertOptions::convert_tests`，见 [ir_converter_main.cc:100](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter_main.cc#L100)）才会把测试一并翻译成 IR。

**练习 2**：`ConvertFilesToPackage` 为什么允许传"多个文件路径"？

> **参考答案**：DSLX 支持跨文件 `import`，一个设计可能拆在多个 `.x` 里。转换时需要把它们一起喂进来才能解析依赖；此时还必须给 `--package_name`（见 [ir_converter_main.cc:143-152](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/ir_converter_main.cc#L143-L152) 的校验）。

---

### 4.3 opt_main：优化 IR

#### 4.3.1 概念说明

`opt_main` 接收一个 IR 文件，跑一遍 XLS 的**标准优化管线（standard optimization pipeline）**，把等价但更精简的 IR 打印出来。优化的目标是：在不改变功能的前提下，减少节点数、缩短关键路径、为后续调度与代码生成铺路。

对 `simple_add.x` 来说，最该被优化掉的就是 `x + y + u32:0` 里的 `+ 0`——因为对任意整数 \(a\) 都有恒等式：

\[
a + 0 = a
\]

这条"加零恒等"正是算术化简 Pass（arith_simplification）会识别并消除的冗余。本讲只需看到效果；哪些 Pass 组成标准管线、它们各自做什么，会在 u4 单元深入。

#### 4.3.2 核心流程

`opt_main` 的执行流程：

```
IR 文本 (simple_add.ir)
        │
        ▼
GetOptFlags(...) → OptOptions         # 优化等级、顶层、跳过的 pass 等
        │
        ▼
OptimizeIrForTop(ir_string, options)  # 解析 IR → 跑优化管线 → 回打成字符串
        │  返回 optimized_ir (string)
        ▼
stdout / --output_path
```

关键点：优化管线是**迭代到不动点（fixedpoint）**的——反复跑各个 Pass，直到 IR 不再变化为止。`opt_main` 还提供了 `--list_passes` 用来列出管线里所有 Pass 的名字（这条会在 u4-l1 详细用到）。

#### 4.3.3 源码精读

`opt_main.cc` 顶部就把职责说清楚了：

[xls/tools/opt_main.cc:57-70](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_main.cc#L57-L70) —— `kUsage`：输入一个 IR 文件，跑标准优化管线，把优化后的 IR 打到 stdout。

`--list_passes` 标志的定义：

[xls/tools/opt_main.cc:77-78](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_main.cc#L77-L78) —— `list_passes` 布尔标志：传了就只列出所有 Pass 名字然后退出。`main` 里对它的处理在 [opt_main.cc:269-282](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_main.cc#L269-L282)。

真正干活的调用在 `RealMain` 里：

[xls/tools/opt_main.cc:230-231](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_main.cc#L230-L231) —— 调 `OptimizeIrForTop(ir, options, &metadata)`，返回优化后的 IR 字符串。注意它吃的是 **IR 文本字符串**（不是文件对象），内部自己解析、优化、再 `DumpIr`。

那个库函数的声明与默认选项：

[xls/tools/opt.h:88-96](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt.h#L88-L96) —— 两个 `OptimizeIrForTop` 重载：一个改 `Package*`，一个吃 IR 文本返回优化后文本（`opt_main` 用的是后者）。

[xls/tools/opt.h:52-53](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt.h#L52-L53) —— `OptOptions::opt_level` 默认值是 `xls::kMaxOptLevel`，所以不指定时 `opt_main` 用的是最激进的那档。

#### 4.3.4 代码实践

1. **实践目标**：用 `diff` 直接看到优化器消除了什么。
2. **操作步骤**：
   ```
   ./bazel-bin/xls/tools/opt_main /tmp/simple_add.ir > /tmp/simple_add.opt.ir
   diff -U8 /tmp/simple_add.ir /tmp/simple_add.opt.ir
   ```
3. **需要观察的现象**：diff 应显示优化后**少了一个 `literal(value=0)` 节点和那个把结果 `+ 0` 的 `add` 节点**，返回值直接指向 `x + y` 的那个 `add`。
4. **预期结果**：优化后 IR 大致变成（**示意，待本地验证**）：
   ```
   package simple_add

   fn add(x: bits[32], y: bits[32]) -> bits[32] {
     add.1: bits[32] = add(x, y)
     ret add.1: bits[32]
   }
   ```
   即从"两个 add + 一个 literal"精简成"一个 add"。
5. **结果**：精确节点编号与 diff 细节**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：除了 `+ 0`，再举一个会被算术化简消除的冗余写法（用 DSLX 表达）。

> **参考答案**：例如 `x * u32:1`（乘 1）、`x - u32:0`（减 0）、`x | u32:0`（或 0）、`x & bits[32]:0xFFFFFFFF`（与全 1）等，都属于恒等冗余，会被对应化简 Pass 消成 `x` 本身。

**练习 2**：怎么知道 `opt_main` 实际跑了哪些 Pass、跑了多少轮？

> **参考答案**：用 `--pass_metrics_path` 把 `OptMetadata.metrics`（含 `total_passes` 等）写到文件（见 [opt_main.cc:233-237](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_main.cc#L233-L237)）；或直接 `opt_main --list_passes` 看管线里有哪些 Pass。

---

### 4.4 codegen_main：从 IR 生成 Verilog

#### 4.4.1 概念说明

`codegen_main` 是工具链的终点：它吃一个（通常已优化的）IR 文件，做**流水线调度**，再生成可综合的 SystemVerilog（`.v`）。它还能顺带写出模块签名（`--output_signature_path`）、调度后的 IR（`--output_schedule_ir_path`）、Block IR（`--output_block_ir_path`）等辅助产物。

它的核心是一个"生成器（generator）"选择：

- `--generator=pipeline`（默认）：生成**带流水线寄存器**的时序模块，需要先做调度。
- `--generator=combinational`：生成**纯组合**模块，没有寄存器、不需要调度。

本讲用的是 `--pipeline_stages=1`，即 1 级流水线。调度的输入是延迟模型（`--delay_model`），它告诉调度器每个运算有多"重"；`unit` 模型让所有运算耗时相同，是最简单的起步选择。调度与延迟模型的原理在 u4 单元深入。

#### 4.4.2 核心流程

`codegen_main` 的流程比前两个工具更复杂，因为它把"调度"和"生成"串在一起：

```
IR 文本 (simple_add.opt.ir)
        │
        ▼
Parser::ParsePackage(...)            # 解析 IR 文本成 Package
        │
        ├─ 如果 generator == pipeline：
        │       Schedule(...)         # 流水线调度，得到 PackageSchedule
        │
        ▼
Codegen(...)                          # Block 转换 + 生成 Verilog 文本
        │  返回 CodegenResult { verilog_text, signature, ... }
        ▼
stdout / --output_verilog_path (.v)
```

两个关键分支值得记住：**只有 pipeline 生成器才需要调度**；combinational 生成器跳过调度，直接做组合逻辑生成（这一点在库函数里也有对应的 `CHECK`，见下）。

#### 4.4.3 源码精读

`codegen_main.cc` 的 `kUsage` 给了两种典型用法：

[xls/tools/codegen_main.cc:47-60](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/codegen_main.cc#L47-L60) —— 用法示例：`--generator=combinational` 出组合模块；`--generator=pipeline --clock_period_ps=... --pipeline_stages=...` 出流水线模块。

`RealMain` 的入口与 IR 解析：

[xls/tools/codegen_main.cc:65-72](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/codegen_main.cc#L65-L72) —— `RealMain` 先读文件、再用 `Parser::ParsePackage` 把 IR 文本重建为内存 `Package`。注意它同样支持 `-` 表示 stdin。

顶层实体处理：

[xls/tools/codegen_main.cc:84-89](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/codegen_main.cc#L84-L89) —— 如果传了 `--top` 且 Package 还没有顶层，就按名字设顶层；并 `XLS_RET_CHECK` 确保一定有顶层（"needs a top function/proc"）。

"只有 pipeline 才调度"的分支：

[xls/tools/codegen_main.cc:99-103](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/codegen_main.cc#L99-L103) —— 当 `generator == GENERATOR_KIND_PIPELINE` 时才调用 `Schedule(...)`；combinational 直接跳过。库函数侧对这两种生成器有明确的 `CHECK_EQ` 区分（见 [codegen.cc:104-119](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/codegen.cc#L104-L119)）。

最终的代码生成调用：

[xls/tools/codegen_main.cc:125-128](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/codegen_main.cc#L125-L128) —— 调 `Codegen(...)` 得到 `CodegenResult`，其中含 `verilog_text`、`signature` 等。

Verilog 文本的输出（stdout 或文件）：

[xls/tools/codegen_main.cc:177-182](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/codegen_main.cc#L177-L182) —— 若没给 `--output_verilog_path`，就把 `verilog_text` 打到 stdout；否则写文件。

支撑的库函数声明：

[xls/tools/codegen.h:35-43](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/codegen.h#L35-L43) —— `Schedule(...)` 与 `Codegen(...)` 的声明，分别对应"调度"和"生成"两步。

相关命令行标志的定义（理解本讲命令里那几个参数从哪来）：

[xls/tools/codegen_flags.cc:63-65](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/codegen_flags.cc#L63-L65) —— `--generator` 标志，默认值 `"pipeline"`，合法值 `pipeline` / `combinational`。

[xls/tools/scheduling_options_flags.cc:129-133](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/scheduling_options_flags.cc#L129-L133) —— `--pipeline_stages`（生成的流水线级数）与 `--delay_model`（延迟模型名）。

> 旁注：codegen 有版本概念。当 Package 用了"Proc 作用域 channel"时，会自动切到 `CODEGEN_VERSION_ONE_DOT_FIVE`（[codegen_main.cc:76-78](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/codegen_main.cc#L76-L78)），本讲的纯函数示例不涉及。

#### 4.4.4 代码实践

1. **实践目标**：生成 Verilog 并读懂它的基本结构。
2. **操作步骤**：
   ```
   ./bazel-bin/xls/tools/codegen_main \
       --pipeline_stages=1 --delay_model=unit \
       /tmp/simple_add.opt.ir > /tmp/simple_add.v
   ```
   然后打开 `/tmp/simple_add.v` 阅读。
3. **需要观察的现象**：模块名（通常取自 Package 名）、一个 `clk` 输入、`x`/`y` 两个 32 位输入端口、一个 32 位输出端口，以及若干寄存器（因为默认 `flop_inputs=true`、`flop_outputs=true`）；核心运算就是 `x + y`。
4. **预期结果**：你会看到一个 SystemVerilog `module ... endmodule`，内部把 `x + y` 的结果寄存一拍后输出。由于本例只有 1 级流水线且默认会 flop 输入/输出，端口与时序细节较丰富——**精确的 Verilog 文本待本地验证**。
5. **结果**：若想看 codegen 还顺带产出了什么，可加 `--output_signature_path=/tmp/simple_add.sig.textproto` 导出模块签名，对照阅读每个端口的宽度与方向。

#### 4.4.5 小练习与答案

**练习 1**：把 `--generator=combinational`（去掉 `--pipeline_stages`）再跑一次，对比和 pipeline 版的 `.v` 有何不同。

> **参考答案**：组合版**没有 `clk`、没有寄存器**，输出直接是 `assign out = x + y;` 这样的纯组合逻辑；pipeline 版则有时钟和流水线寄存器。这正对应源码里 `generator == pipeline` 才走调度、才插寄存器的分支（4.4.3）。

**练习 2**：为什么 codegen 需要延迟模型，而 `opt_main` 不需要？

> **参考答案**：优化（opt）关心的是"图等价变换、减少冗余"，与时间无关；而 codegen 的流水线调度要把运算分配到时钟级，必须知道每个运算的延迟才能判断"一拍内放得下多少运算"。所以延迟模型是调度（codegen）的输入，不是优化的输入。

---

## 5. 综合实践

把本讲四步串成一个完整任务，亲手走通"从一段 DSLX 到一块 Verilog"。

**任务**：新建 `/tmp/simple_add.x`（含一个可被优化掉的 `+ u32:0`），依次执行解释、IR 转换、opt 优化、codegen 生成 1 级流水线 Verilog，用 `diff` 观察优化前后 IR 的变化，并阅读生成的 `.v` 文件。

**步骤**：

1. 创建 `/tmp/simple_add.x`，内容照搬 4.1.2 的示例（函数 + 测试）。
2. 解释执行，确认测试通过：
   ```
   ./bazel-bin/xls/dslx/interpreter_main /tmp/simple_add.x
   ```
3. 转 IR：
   ```
   ./bazel-bin/xls/dslx/ir_convert/ir_converter_main --top=add /tmp/simple_add.x > /tmp/simple_add.ir
   ```
4. 优化并对比：
   ```
   ./bazel-bin/xls/tools/opt_main /tmp/simple_add.ir > /tmp/simple_add.opt.ir
   diff -U8 /tmp/simple_add.ir /tmp/simple_add.opt.ir
   ```
   预期：优化后少了 `literal(value=0)` 与那个 `+ 0` 的 `add`。
5. 生成 Verilog 并阅读：
   ```
   ./bazel-bin/xls/tools/codegen_main --pipeline_stages=1 --delay_model=unit /tmp/simple_add.opt.ir > /tmp/simple_add.v
   ```
   打开 `/tmp/simple_add.v`，找出模块名、时钟端口、数据端口与核心 `+` 运算。
6. （进阶）换 `--generator=combinational` 再生成一份，与 pipeline 版做对比，体会"组合 vs 流水线"在 Verilog 上的差异。

**检验标准**：

- `/tmp/` 下齐备 `.ir`、`.opt.ir`、`.v` 三个产物。
- 能用自己的话说出 diff 里"少了什么、为什么"。
- 能在 `.v` 中指认出 `x + y` 这条核心逻辑落在哪一行。

> 全部命令的精确输出依本机 XLS 版本与默认标志而定，**待本地验证**。

---

## 6. 本讲小结

- XLS 的命令行工作流是一条单向流水线：`interpreter_main`（跑测试）+ `ir_converter_main`（`.x→.ir`）+ `opt_main`（`.ir→.opt.ir`）+ `codegen_main`（`.opt.ir→.v`）。
- `interpreter_main` 不落盘，只验证 DSLX 逻辑；后三个工具才是会产出中间文件的转换链。
- 三个转换工具都是"薄壳"，核心逻辑在库函数里：`ConvertFilesToPackage`、`OptimizeIrForTop`、`Schedule`/`Codegen`。
- `opt_main` 跑标准优化管线到不动点，能消除 `+ 0` 这类恒等冗余，用 `diff` 可直接看到 IR 变精简。
- `codegen_main` 的 `--generator` 决定走不走调度：`pipeline` 需要调度+插寄存器，`combinational` 是纯组合；延迟模型是调度的输入。
- 本讲命令蓝本来自官方 `docs_src/tools_quick_start.md`，遇到不确定的输出以本地运行为准。

---

## 7. 下一步学习建议

本讲把命令链跑通了，但每一步内部都还有很多可以深挖的地方。建议按以下顺序继续：

1. **想看 IR 到底长什么样、怎么读写** → 进入 u3 单元，从 u3-l1《IR 总览：Package、Function、Node、Value》和 u3-l3《IR 文本格式：解析与打印》开始。
2. **想知道 `opt_main` 跑了哪些 Pass** → u4-l1《优化 Pass 框架》会讲管线编排，配合 `opt_main --list_passes` 实操。
3. **想理解 codegen 怎么把 IR 变成带寄存器的 Verilog** → u5 单元《代码生成：从 IR 到 Verilog》，尤其是 u5-l1 的 Block 转换与 u5-l4 的 VAST。
4. **想用 Bazel 把整条链自动串起来** → u7-l2《Bazel 构建规则与宏》，学会用 `dslx_test`、codegen 规则替代手工敲命令。
5. **继续阅读源码**：可先读 `xls/tools/codegen_main.cc` 的 `RealMain` 全貌，再顺着 `Schedule`/`Codegen` 往下追，建立"命令→库函数→底层机制"的完整心智模型。
