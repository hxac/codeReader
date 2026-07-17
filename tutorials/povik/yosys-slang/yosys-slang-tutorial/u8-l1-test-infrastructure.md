# 测试体系：等价性检查与自测

## 1. 本讲目标

本讲是单元 8（测试、构建与二次开发）的第一篇。前面七讲我们一直在读「sv-elab 如何把 SystemVerilog 翻译成 RTLIL」，但翻译正确与否靠什么保证？答案就在 `tests/` 目录里。

学完本讲，你应该能够：

- 说清 sv-elab 测试目录的整体结构，区分 `unit/`、`various/` 两类测试与 `.ys`、`.tcl`、`.sv` 三种文件。
- 掌握「等价性测试」这条主线：用 `read_slang` 生成「gate 网表」，用 `read_rtlil`/`read_verilog` 生成「gold 网表」，再用 `equiv_make`/`equiv_induct`/`equiv_status -assert` 证明两者功能等价。
- 理解 CTest 如何把每个 `.ys` 注册成一个测试用例，以及 `run.sh` 这个独立脚本的互补角色。
- 了解两个内置自测命令 `test_slangexpr` 与 `test_slangdiag`，它们分别用于「表达式求值自检」和「负向诊断测试」。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么叫「综合正确」？** sv-elab 是一个翻译器：输入 SystemVerilog，输出 RTLIL 网表。翻译可能出错（少建一个单元、接反一根线、把异步复位当成同步……）。要验证翻译正确，最稳的办法不是「看输出像不像」，而是「与一个已知正确的参考实现做功能等价证明」。这正是 Yosys 的 `equiv_*` 系列命令擅长的事。

**gate 与 gold。** 等价性测试里有两个人物：

- **gate**（被测对象）：用 `read_slang` 读入 SystemVerilog，是我们要验证的「新网表」。
- **gold**（参考实现）：用 `read_rtlil` 手写一份期望的 RTLIL，或用 `read_verilog` 让 Yosys 自带的 Verilog 前端生成。它是「标准答案」。

只要证明 gate 和 gold 在所有输入下输出一致，就说明 sv-elab 这次翻译没问题。

**两种自测命令。** 不是所有东西都能用等价性测。有两类东西需要专门的自测命令：

- 表达式求值的正确性（`$add`、`$mux` 等单元发出的值是否和 slang 自己算出来的常量一致）——交给 `test_slangexpr`。
- 「应当报错时确实报了正确的错」（负向测试）——交给 `test_slangdiag`。

> 本讲假设你已经读过 u2-l1（`read_slang` 命令与前端注册）和 u3-l2（RTLIL 单元），至少知道 `$dff`/`$dffe`/`$add` 是什么。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tests/CMakeLists.txt` | 把所有 `.ys`/`.tcl` 测试脚本注册成 CTest 用例，并可选地配置 Clang 覆盖率收集 |
| `tests/run.sh` | 不依赖 CTest 的独立 bash 跑测脚本，逐个跑 `*/*.ys` 和 `*/*.tcl`，打印红绿结果 |
| `tests/unit/dff.ys` | 触发器等价性测试的范本：gate（`read_slang`）+ gold（`read_rtlil`/`read_verilog`）+ `equiv_*` 证明 |
| `tests/unit/selftests.tcl` | Tcl 脚本：用 `sat -verify -prove-asserts` 对一批 `.sv` 做可满足性证明，并对比「展平」与「保留层次」两种模式 |
| `tests/various/expr.sv` + `expr.ys` | `test_slangexpr` 自测样本：用 `$t(expr)` 标注每个待检表达式 |
| `tests/various/readmem_diag.ys` | `test_slangdiag -expect "..."` 负向测试样本 |
| `src/slang_frontend.cc` | 内含 `TestSlangDiagPass`、`TestSlangExprPass` 两个自测 Pass，以及 `check_diagnostics`、`expected_diagnostic` 等负向测试基础设施 |

## 4. 核心概念与源码讲解

### 4.1 测试目录的总体结构

#### 4.1.1 概念说明

打开 `tests/`，你会看到两个子目录和几种文件后缀，先理清它们的分工：

- `unit/`：**单元测试**。每个文件聚焦一个语言构造，用最精简的例子验证一类行为，例如 `dff.ys`（触发器）、`async.ys`（异步复位）、`latch.ys`（锁存器）、`function_call.ys`（函数调用）、`dualedge.ys`（双沿触发）。
- `various/`：**杂项/回归测试**。覆盖面广，多为针对具体 issue 或特性的测试，例如 `issue142.ys`、`mem_inference.ys`、`concurrent_assert.ys`、`readmem.ys`。

文件后缀有三种：

- `.ys`：Yosys 脚本，是绝对主力，绝大多数测试都是它。
- `.tcl`：Tcl 脚本，靠 `yosys -import` 把 Yosys 命令引入 Tcl 命名空间后编写更复杂的循环/分支逻辑（见 `selftests.tcl`）。
- `.sv`：被测的 SystemVerilog 源码，由 `.ys`/`.tcl` 间接 `read_slang` 读入，或由自测命令直接消费（如 `expr.sv`）。

#### 4.1.2 核心流程

一条 `.ys` 测试的生命周期是：

1. CTest（或 `run.sh`）调用 `yosys -m slang.so testcase.ys`。
2. Yosys 加载 `slang.so` 插件，注册出 `read_slang` 等命令（见 u2-l1）。
3. 逐行执行脚本：`read_slang` 生成 gate 网表，随后做等价性证明或断言检查。
4. 脚本中任何 `log_error` 或 `equiv_status -assert` 失败都会让 Yosys 以非零码退出，CTest 据此判定 FAIL。

#### 4.1.3 源码精读

整个测试清单集中定义在一个 CMake 列表里。[tests/CMakeLists.txt:26-78](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/CMakeLists.txt#L26-L78) 把 `unit/*.ys`、`various/*.ys` 与 `unit/selftests.tcl` 统一收进 `ALL_TESTS`。这个列表就是「项目目前覆盖了哪些语言特性」的一张总目录。

随后一个 `foreach` 把每个脚本注册成一个 CTest 用例：

[tests/CMakeLists.txt:80-90](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/CMakeLists.txt#L80-L90) 逐个 `add_test`，工作目录设为脚本所在目录（这样 heredoc 里写相对路径、`$readmemh` 读相对文件都能正常工作），命令行是 `yosys -m <slang.so 路径> <脚本全路径>`。其中 `$<TARGET_FILE:yosys-slang>` 是 CMake 的生成器表达式，在构建期替换成插件的实际产物路径。

> 小细节：清单里 `unit/selftests.tcl` 是唯一的 `.tcl`，与一堆 `.ys` 并列注册，说明 CTest 不在乎后缀，只在乎「`yosys` 能不能执行它」。

#### 4.1.4 代码实践

**目标**：建立对测试规模与分类的直观印象。

1. 打开 [tests/CMakeLists.txt](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/CMakeLists.txt)。
2. 数一下 `ALL_TESTS` 里 `unit/` 与 `various/` 各有多少项，再统计有多少 `issue*.ys`（这类通常是回归测试，每条对应一个已修 bug）。
3. 挑一条 `various/issueNNN.ys`，读它的 `read_slang` heredoc，猜测它当年修的是什么问题。

**需要观察的现象**：你会看到 `various/` 远多于 `unit/`，且 `issue*` 占比不小——这说明 sv-elab 的测试以「特性驱动 + 回归驱动」为主。

**预期结果**：能口头说出「unit 是按构造分类的最小用例，various 是按特性/issue 聚合的回归用例」。待本地验证具体计数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CTest 的 `add_test` 要把 `WORKING_DIRECTORY` 设成脚本所在目录，而不是统一用构建目录？

**参考答案**：因为很多测试的 heredoc 或 `$readmemh` 会引用相对路径的数据文件（例如 `readmem_diag.ys` 依赖 `.hex`/`.bin` 文件），这些文件就放在脚本旁边。设成脚本所在目录才能让相对路径正确解析。

**练习 2**：如果想新增一个测试 `various/myfeature.ys`，最少要改几处？

**参考答案**：只要把 `various/myfeature.ys` 加进 [tests/CMakeLists.txt](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/CMakeLists.txt#L26-L78) 的 `ALL_TESTS` 列表即可，`foreach` 会自动把它注册成 CTest 用例。

---

### 4.2 等价性测试范式：gate 对 gold

#### 4.2.1 概念说明

这是 sv-elab 测试体系的**核心思想**，理解了它就读懂了一半的 `.ys`。

我们把 sv-elab 生成的网表叫 **gate**（被测），把参考实现叫 **gold**（标准答案）。证明二者等价的标准三步是：

1. `equiv_make gold gate equiv`：构造一个「比较模块」`equiv`，把 gold 和 gate 的对应输入并联、对应输出接到一个「等价检查单元」（miter）上。只要两个设计的某个输出在任何时刻不一致，这个 miter 就能被满足。
2. `equiv_induct equiv`：对 `equiv` 做 **k-归纳证明**（k-induction）。它先证明「连续 k 个周期两者输出都一致能推出第 k+1 个周期也一致」，再结合基数情况，从而证明对所有输入序列永远一致。
3. `equiv_status -assert`：检查证明结果。`-assert` 表示「如果还有任何等价单元未被证明（UNPROVEN）或不可达（UNREACHED），就让脚本报错退出」。

只要 `equiv_status -assert` 不报错，这次翻译就被认为是正确的。

> 为什么需要 `async2sync`？异步复位寄存器（`$adff`/`$aldffe`）的复位行为发生在时钟沿之外，直接做归纳证明比较麻烦。`async2sync` 会把异步复位改写成同步形式，让 gold 和 gate 站在同一条「每个时钟沿比较一次」的起跑线上。

#### 4.2.2 核心流程

一个典型的等价性测试脚本结构如下（伪代码）：

```
read_slang <<EOF          # 生成 gate：被测的 sv-elab 输出
module <name>_gate(...);
    ...
endmodule
EOF

read_rtlil <<EOF          # 或 read_verilog：生成 gold 参考实现
module \<name>_gold;
    ...手写期望的 RTLIL 单元...
end
EOF

async2sync                # 异步复位归一化
equiv_make gold gate equiv
equiv_induct equiv        # 归纳证明
equiv_status -assert      # 必须全证毕

design -reset             # 清空，进入下一个子测试
```

注意每个 `.ys` 文件里常常**塞了多个子测试**，用 `design -reset` 隔开——这样一个 CTest 用例就能覆盖一族相关构造。

#### 4.2.3 源码精读

以 `tests/unit/dff.ys` 第一个子测试为例。**gate** 是用 `read_slang` 读入的一段 SystemVerilog：

[tests/unit/dff.ys:1-8](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L1-L8) 描述了一个带 `iff` 使能条件的 `always_ff`：`q <= d + 1`。这正是 u3-l2 里 `$add` 与 `$dffe` 的活样本——sv-elab 要把这条 SV 翻译成一个 `$add`（算 `d+1`）接一个 `$dffe`（带使能的触发器）。

**gold** 是手写的期望 RTLIL，逐个单元、逐根线地「点名」：

[tests/unit/dff.ys:10-39](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L10-L39) 里你能直接看到期望的 `$add` 单元（`A=\d`、`B=1'1`、`Y=$1`）和 `$dffe` 单元（`D=$1`、`Q=\q`、`EN=\en`、`CLK=\clk`）。这相当于「我期望 sv-elab 生成这些单元」。注意这里的 gold 用 `read_rtlil` 直接吃 RTLIL 文本，跳过了任何前端，是最硬核的「标准答案」。

随后四行就是证明流水线：

[tests/unit/dff.ys:41-44](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L41-L44) 依次 `async2sync`（把 `$dffe` 的使能形式归一）、`equiv_make` 建 miter、`equiv_induct` 归纳证明、`equiv_status -assert` 断言全证毕。

文件后半段还有几个变体值得注意：

- 第二个子测试（line 47 起）用一个 1 位的 `always`（非 `always_ff`）验证同样的 `iff` 语义。
- 第三个子测试（line 81 起）gold 改用 **`read_verilog`** 而非 `read_rtlil`——让 Yosys 自带 Verilog 前端当参考实现，省去手写 RTLIL。注意此时 gold 多了一步 `proc`（把 Verilog 的 `always` 过程块降级成单元），与 gate 侧的 `async2sync` 对齐。
- 第四、五个子测试（line 111、151 起）验证 `$past`——sv-elab 会把 `$past(d)` 翻译成两个串起来的 `$dff`/`$dffe`（见 u7-l4），gold 里能直接看到 `$$past$1` 这根中间线。

#### 4.2.4 代码实践

**目标**：亲手读懂一个等价性测试，并新增一个最小用例。

1. 通读 [tests/unit/dff.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys)，对每个子测试画出「gate 的 SV → gold 的 RTLIL 单元 → equiv 证明」三栏对照表。
2. 在文件末尾仿照格式新增一个最小用例，例如验证一个纯组合的与门：

   ```
   design -reset
   read_slang <<EOF
   module and_gate(input logic [3:0] a, input logic [3:0] b, output logic [3:0] y);
       assign y = a & b;
   endmodule
   EOF

   read_rtlil <<EOF
   module \and_gold
       wire width 4 input 1 \a
       wire width 4 input 2 \b
       wire width 4 output 3 \y
       cell $and $1
           parameter \A_SIGNED 0
           parameter \B_SIGNED 0
           parameter \Y_WIDTH 4
           connect \A \a
           connect \B \b
           connect \Y \y
       end
   end
   EOF

   equiv_make and_gold and_gate and_equiv
   equiv_induct and_equiv
   equiv_status -assert
   ```

   （本段为**示例代码**，未在仓库中运行过；组合逻辑无需 `async2sync`。）

3. 运行 `ctest -R dff` 看现有用例是否通过；再运行你新增的脚本（`yosys -m build/slang.so tests/unit/dff.ys`）。

**需要观察的现象**：`equiv_status` 应输出类似 `Equivalence successfully proven` 之类的总结，且最后那行 `-assert` 不触发 `log_error`。

**预期结果**：理解「gate 是 read_slang 产物、gold 是参考、equiv_* 做证明」。若新增用例的 gold 写错了单元类型（例如把 `$and` 写成 `$or`），`equiv_status -assert` 会失败——这正是等价性测试能抓住翻译错误的原因。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dff.ys` 第一个子测试需要 `async2sync`，而纯组合的与门用例不需要？

**参考答案**：前者含触发器（`$dffe`），且 `iff` 使能与异步语义相关，需要把时序逻辑归一化后才能在「每个时钟沿比较一次」的框架下做归纳证明；与门是纯组合逻辑，没有时钟和复位，直接构造 miter 比较输出即可，无需 `async2sync`。

**练习 2**：第三个子测试用 `read_verilog` 而非 `read_rtlil` 写 gold，并多加了一步 `proc`。为什么？

**参考答案**：`read_verilog` 读入的是行为级 Verilog（含 `always` 过程块），需要 `proc` 把过程块降级成 RTLIL 单元，才能与 gate 侧（已经过 sv-elab 翻译、同样需要降级对齐）站在同一抽象层做比较。`read_rtlil` 直接吃网表则不需要 `proc`。

**练习 3**：`equiv_status -assert` 失败意味着什么？是「sv-elab 一定有 bug」吗？

**参考答案**：不一定。它意味着「gold 与 gate 没能被证明等价」，可能是 sv-elab 翻译有 bug，也可能是 gold 写错了（参考答案本身不对），还可能是归纳证明的深度不够（可尝试加大 `equiv_induct -seq N`）。需要人工复核两边网表来定位。

---

### 4.3 CTest 集成与 run.sh 脚本

#### 4.3.1 概念说明

注册成 CTest 用例（4.1 已见）只是「把测试登记进构建系统」。真正「跑测试」有两条互补的路径：

- **CTest**（`ctest`/`make test`）：标准、可并行、能接 CI、能统计覆盖率，是正式入口。
- **run.sh**：一个独立的 bash 脚本，不依赖 CMake/CTest，直接 `for` 循环跑所有 `*/*.ys` 和 `*/*.tcl`，用红绿颜色打印每个用例的 OK/FAIL。适合本地快速跑一遍或在没有 CTest 的环境里用。

#### 4.3.2 核心流程

`run.sh` 的逻辑很直白：枚举 `tests/*/*.ys` 和 `tests/*/*.tcl`，对每个文件 `cd` 到其目录后执行 `yosys -m slang.so <文件>`，把输出重定向到 `/dev/null`；若退出码非零就标红 FAIL 并复跑一次（带 `-g -Q` 显示日志）方便排错，否则标绿 OK。任何一个用例失败，脚本最终的退出码就是 1。

#### 4.3.3 源码精读

[tests/run.sh:11-26](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/run.sh#L11-L26) 是主循环。第 11 行的 glob `"$TESTSDIR"/*/*.ys "$TESTSDIR"/*/*.tcl` 正好覆盖 `unit/` 和 `various/` 两个子目录下的全部脚本。

关键调用在第 17 行：

[tests/run.sh:17](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/run.sh#L17) 在子 shell 里 `cd` 到脚本目录（保证相对路径正确），用 `exec` 启动 `yosys -m "$PLUGIN"`。`$PLUGIN` 在脚本开头第 4 行被写死成 `../build/slang.so` 的绝对路径，所以 `run.sh` 假设你已经在仓库根目录用 `cmake -B build && make -C build` 构建过插件。

回到 CTest 侧，覆盖率收集是一个可选的高级特性。[tests/CMakeLists.txt:1-24](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/CMakeLists.txt#L1-L24) 在 `WITH_COVERAGE` 且编译器为 Clang 时，给每个测试注入 `LLVM_PROFILE_FILE` 环境变量（通过 `test_launcher`），并定义一个 `coverage` 目标，用 `llvm-profdata` 合并、`llvm-cov` 导出 lcov 报告——注意它特意 `--ignore-filename-regex=/third_party/` 把 slang 等第三方库排除在统计之外。

#### 4.3.4 代码实践

**目标**：分别用 CTest 与 `run.sh` 跑一遍测试，对比体验。

1. 先按 README 构建插件：`cmake -B build . && make -C build -j$(nproc)`。
2. 用 CTest：`cd build && ctest -R dff -V`（`-V` 显示日志，`-R` 只跑名字含 `dff` 的用例）。
3. 用 `run.sh`：回到仓库根，`bash tests/run.sh`（或 `./tests/run.sh`），观察红绿输出。

**需要观察的现象**：`run.sh` 会逐行打印 `tests/unit/dff.ys... OK` 这样的着色结果；CTest 则汇总成 `X% tests passed, Y tests failed out of Z`。

**预期结果**：两种方式跑出来的 FAIL 集合应一致。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`run.sh` 第 17 行失败后会复跑一次（第 20 行带 `-g -Q`）。为什么不直接在第一次就显示日志？

**参考答案**：为了在「全部通过」时保持输出干净（首次把 stdout/stderr 都丢进 `/dev/null`），只在出现失败时才把带详细日志的复跑结果打印出来，方便定位。

**练习 2**：CTest 用例名（`add_test(NAME ...)`）取的是脚本相对路径如 `unit/dff.ys`。这给 `ctest -R` 带来什么便利？

**参考答案**：可以用正则按目录或文件名筛选，例如 `ctest -R unit/` 只跑单元测试，`ctest -R various/issue` 只跑回归测试，便于聚焦调试。

---

### 4.4 自测命令 test_slangexpr 与 test_slangdiag

#### 4.4.1 概念说明

等价性测试适合「整个模块的行为」，但有两类东西测起来不顺手：

1. **单个表达式的求值正确性**：sv-elab 把 `a + b`、`|x`、`a[i]` 翻译成 RTLIL 单元后，计算结果对不对？写一个完整模块来做等价证明太重了。于是有了 `test_slangexpr`：它把每个待检表达式包进一个伪系统函数 `$t(...)`，对同一表达式求两次值——一次走 slang 的常量求值（参考答案），一次走 sv-elab 实际建单元后的求值（被测）——然后比较。
2. **诊断消息的正确性**：「这段 SV 应该触发『某条错误』」是负向测试。直接 `read_slang` 会让错误中止脚本，需要一个机制声明「我预期会出现这条消息，出现了反而算通过」。这就是 `test_slangdiag -expect "..."`。

这两个命令都是 sv-elab 自己注册的 Yosys Pass，定义在 `src/slang_frontend.cc` 末尾。

#### 4.4.2 核心流程

**`test_slangexpr <file.sv>`** 的流程（见 `TestSlangExprPass::execute`）：

1. 复用 slang driver 解析 `<file.sv>`、创建 Compilation、上报诊断（与 `read_slang` 共享同一套前置流程，见 u2-l2）。
2. 注册一个自定义系统函数 `$t`（类 `TFunc`）：它的 `eval` 恒返回「非常量」（`notConst`），迫使 slang 在常量求值时**不**把 `$t(...)` 折叠掉。
3. 遍历顶层，找到所有 `$t(expr)` 调用，对 `expr` 求两次值：
   - `ref = netlist.eval(*expr)`：走 sv-elab 的 EvalContext，**允许**常量折叠捷径（先用 slang 常量求值）——作为参考。
   - `test = amended_eval(*expr)`：同一个 EvalContext，但 `ignore_ast_constants = true`，**禁止**常量折叠，逼它真的把 RTLIL 单元建出来再算——作为被测。
4. 比较 `ref == test`，逐条打印，最后若有失败就用 `log_error` 中止。

> 直觉：参考值来自「能折叠就折叠」的捷径，被测值来自「真刀真枪建单元」。两者一致，说明 sv-elab 的单元映射与 slang 的常量语义吻合。

**`test_slangdiag -expect "msg"`** 的流程：

1. 把期望消息文本存进一个文件级静态变量 `expected_diagnostic`。
2. 随后执行 `read_slang`。在 `read_slang` 内部，每拿到一批诊断就调 `check_diagnostics` 比对**格式化后的完整文案**（不是诊断码）。
3. 若命中，`check_diagnostics` 返回 true，`read_slang` 把 `in_succesful_failtest` 置 true，从而「吞掉」原本会让脚本中止的 `log_error`——带错的测试也算通过。
4. 若直到最后都没命中，`check_diagnostics(..., /*last=*/true)` 直接 `log_error` 让测试失败。

#### 4.4.3 源码精读

先看负向测试的基础设施。文件级静态变量声明在：

[src/slang_frontend.cc:3524](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3524) 定义 `static std::string expected_diagnostic;`。`test_slangdiag` 的 `-expect` 参数最终就写到它这里（见 [src/slang_frontend.cc:3913](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3913)）。

核心比对函数带详细注释，把三种情形说得很清楚：

[src/slang_frontend.cc:3655-3672](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3655-L3672) `check_diagnostics` 处理三种情况：(1) 正常模式（无期望）直接返回 false 继续；(2) 测试模式且命中期望，清空期望并返回 true 提前成功；(3) 测试模式未命中——若 `last` 表示「这是最后一批诊断了」就用 `log_error` 判失败，否则返回 false 等下一批。

这套机制在 `SlangFrontend::execute` 里被三处调用（编译期诊断、各 netlist 的翻译期诊断、最后兜底），命中任一处都把 `in_succesful_failtest` 置 true：

[src/slang_frontend.cc:3733-3737](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3733-L3737) 是第一处：先置 `in_succesful_failtest = false`，再用 `check_diagnostics` 检查 `compilation->getAllDiagnostics()`。紧接着若 `getNumErrors()` 非零，只有 `!in_succesful_failtest` 时才 `log_error` 中止——这就是「带错测试也算通过」的开关。

两个自测 Pass 的注册与 `read_slang` 同源，都在 `src/slang_frontend.cc` 末尾：

[src/slang_frontend.cc:3892-3920](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3892-L3920) 是 `TestSlangDiagPass`，注册名为 `test_slangdiag`。它的 `execute` 很简单：解析 `-expect "..."`（去掉首尾引号）写入 `expected_diagnostic`，其余参数交给 `extra_args`。

[src/slang_frontend.cc:3942-4052](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3942-L4052) 是 `TestSlangExprPass`，注册名为 `test_slangexpr`。它内部几乎重放了一遍 `read_slang` 的前置流程（driver、settings、fixup_options、parseAllSources、createCompilation），但**不**把结果写进当前 design 的 RTLIL，而是建一个临时 `NetlistContext` 专门用来求值。关键的双重求值在：

[src/slang_frontend.cc:4026-4027](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L4026-L4027) `ref = netlist.eval(*expr)` 走带常量折叠的参考路径，`test = amended_eval(*expr)` 走 `ignore_ast_constants=true` 的「强制建单元」路径。两者相等才算通过，否则累计 `nfailures`，最后若非零就 `log_error`。

负向测试的用法样本见 `readmem_diag.ys`：

[tests/various/readmem_diag.ys:1-8](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/readmem_diag.ys#L1-L8) 先 `test_slangdiag -expect "failed to open file 'no_such_file_anywhere.hex'"`，再 `read_slang` 一个会触发「打开文件失败」的 SV。因为期望命中，即便 `readmemh` 报错，整个用例仍判通过。注意比较的是**完整文案字符串**，所以期望文本必须和 [src/diag.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc) 里登记的文案逐字符一致（见 u2-l4）。

表达式自测的样本见 `expr.sv`：

[tests/various/expr.ys:1](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/expr.ys#L1) 只有一行 `test_slangexpr expr.sv`。而 [tests/various/expr.sv:17](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/expr.sv#L17) 起的 `$t(f1())`、`$t(-7)`、`$t(|3'd1)` 就是每个被双重求值的待检表达式——文件开头的注释精确描述了「求两次值并比对」的意图。

#### 4.4.4 代码实践

**目标**：亲手写一个最小的负向诊断测试和一个表达式自测样本。

1. 读 [tests/various/readmem_diag.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/readmem_diag.ys)，挑一条 `test_slangdiag -expect "..."`，到 [src/diag.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc) 里找到对应文案登记处，确认字符串逐字一致。
2. 新建一个最小表达式样本（**示例代码**，未在仓库运行）：
   ```
   # myexpr.ys
   test_slangexpr myexpr.sv
   ```
   ```
   // myexpr.sv
   module top;
       initial begin
           $t(4'hf + 4'h1);   // 期望溢出截断为 0
           $t(&4'hf);          // 期望归约与为 1
       end
   endmodule
   ```
   运行 `yosys -m build/slang.so myexpr.ys`，观察末尾的 `N tests passed.`。

**需要观察的现象**：`test_slangexpr` 会逐条打印 `ref == test` 的比对行，最后给出通过/失败计数；若把 `$t(4'hf + 4'h1)` 故意改成会暴露 bug 的表达式，计数里会出现失败。

**预期结果**：理解「`-expect` 比对的是文案字符串、`$t` 触发双重求值」。待本地验证 `myexpr.ys` 的输出。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `test_slangexpr` 要给 `$t` 注册一个 `eval` 恒返回 `notConst` 的系统函数（`TFunc`）？

**参考答案**：如果 slang 能把 `$t(expr)` 整体当成常量折叠掉，就无从触发「对 expr 建单元再求值」的被测路径。让 `$t` 的求值结果恒为非常量，等于插了一个「常量折叠无法穿透」的屏障，强制后续流程真正处理 `expr`。

**练习 2**：`check_diagnostics` 比较的是诊断的「格式化文案」而不是「诊断码」。这样做的优缺点各是什么？

**参考答案**：优点是文案对人直观、测试即文档，且能顺带抓住文案被误改的回归；缺点是文案里任何标点、措辞调整（即便是无害的润色）都会让期望失配，维护成本较高，且无法区分两个恰好文案相同的诊断码。

**练习 3**：`test_slangdiag` 之后若忘记写 `read_slang`，会发生什么？

**参考答案**：`expected_diagnostic` 被设了值却没有任何诊断可比对。在随后任意一次 `read_slang`（哪怕属于别的子测试）的末尾，`check_diagnostics(..., /*last=*/true)` 找不到匹配就会 `log_error`，导致测试失败。因此 `-expect` 必须紧跟在会触发它的那次 `read_slang` 之前。

---

### 4.5（补充）selftests.tcl：SAT 证明与层次对比

#### 4.5.1 概念说明

`tests/unit/selftests.tcl` 是唯一的 `.tcl` 测试，展示了与 `equiv_*` 不同的另一种验证思路：**SAT 可满足性证明**。它不再与 gold 对照，而是直接对 gate 设计做性质检查——把所有断言（`assert`/`assume`）当成要证明的性质，用 SAT 求解器找有没有反例。

它还顺带对比两种 `read_slang` 模式：默认的「展平」与 `--keep-hierarchy` 的「保留层次」，确保层次展平不改变功能（见 u7-l2）。

#### 4.5.2 源码精读

[tests/unit/selftests.tcl:8-26](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/selftests.tcl#L8-L26) 遍历 `unit/*.sv`，`read_slang --infer-input-ports-as-vars` 读入后 `check -assert`（校验网表自洽），再用 `chformal -lower` 把形式化单元降级，最后对每个模块 `setundef -undriven -undef`（把未驱动线设为 x）后 `sat -verify -enable_undef -prove-asserts -show-public` 证明所有断言成立。

[tests/unit/selftests.tcl:29-48](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/selftests.tcl#L29-L48) 是第二遍，改用 `read_slang --keep-hierarchy`，并对每个模块先 `flatten` 再做同样的 SAT 证明。两遍结果都应通过，说明「展平 vs 保留层次」功能等价。

> 这段是补充内容，本讲主线仍是等价性测试与两个自测命令。`selftests.tcl` 的 SAT 思路与 u7-l4 的 SVA 断言翻译呼应：sv-elab 把 `assert` 翻成 `$check`，这里就是去证明这些 `$check` 永不违例。

## 5. 综合实践

把本讲三个最小模块串起来，完成一个小任务：

**任务**：为 sv-elab 新增一条最小的**回归等价性测试**，并理解它如何被 CTest 收录、如何用 `run.sh` 复跑。

1. **选定构造**：选一个简单构造，例如「带同步复位的 8 位计数器」或「`a & b | c` 的组合逻辑」。这里以组合逻辑 `(a & b) ^ c` 为例。
2. **写 gate**：用 `read_slang` heredoc 给出 SystemVerilog。
3. **写 gold**：用 `read_rtlil` 手写期望单元（`$and` 接 `$xor`），或用 `read_verilog` 让 Yosys 自带前端生成。
4. **写证明**：`equiv_make` → `equiv_induct` → `equiv_status -assert`。
5. **注册**：把文件名加进 [tests/CMakeLists.txt](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/CMakeLists.txt#L26-L78) 的 `ALL_TESTS`。
6. **跑测**：先 `yosys -m build/slang.so tests/various/myxor.ys` 单独跑通；再 `ctest -R myxor`；最后 `bash tests/run.sh` 看它出现在绿条里。
7. **（可选）负向验证**：故意把 gold 的 `$xor` 改成 `$xnor`，确认 `equiv_status -assert` 报 FAIL，从而确信这条测试「真的能抓错」。

完成本任务后，你应该能独立为 sv-elab 的任意新构造补一条回归测试，这正是项目维护者接收贡献时最看重的工程习惯之一。

## 6. 本讲小结

- sv-elab 的正确性主要靠「等价性测试」保证：gate（`read_slang` 产物）对 gold（`read_rtlil`/`read_verilog` 参考实现），用 `equiv_make`/`equiv_induct`/`equiv_status -assert` 做归纳证明。
- 测试脚本集中在 `tests/unit/`（按构造分类的最小用例）与 `tests/various/`（特性与 issue 驱动的回归用例），由 `tests/CMakeLists.txt` 的 `ALL_TESTS` 列表统一注册成 CTest 用例。
- CTest 是正式入口（可并行、可统计 Clang 覆盖率），`tests/run.sh` 是不依赖 CMake 的独立红绿跑测脚本，二者互补。
- `test_slangexpr` 用 `$t(expr)` 对表达式做「常量折叠 vs 强制建单元」双重求值比对，验证表达式映射正确。
- `test_slangdiag -expect "..."` 配合 `check_diagnostics` 实现「带错也算通过」的负向诊断测试，比对的是格式化文案字符串而非诊断码。
- `equiv_status -assert` 失败不等于「sv-elab 必有 bug」，需复核 gold 是否写对、归纳深度是否足够。

## 7. 下一步学习建议

- 想看真实大型设计如何端到端验证 sv-elab？读 **u8-l2（croc_boot 集成测试）**，它用一个真实的 RISC-V 内核做综合-启动验证。
- 想理解构建产物（`slang.so`、覆盖率 target）如何被 CMake/Bazel 配出来？读 **u8-l3（构建系统深入）**。
- 准备给项目贡献新构造？读 **u8-l4（扩展开发）**，它会讲清「定位触发点 → 加 handle → 补一条等价性测试」的最小改动闭环——本讲教你的「新增回归用例」正是其中关键一环。
- 建议继续精读的源码：[tests/unit/async.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/async.ys)（异步复位等价证明）、[tests/unit/latch.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/latch.ys)（锁存器）、以及 `src/slang_frontend.cc` 末尾两个自测 Pass 的完整实现。
