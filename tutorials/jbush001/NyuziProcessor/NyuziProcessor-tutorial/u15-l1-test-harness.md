# 测试框架与 CHECK 机制

## 1. 本讲目标

本讲是「验证体系与测试方法」单元的第一讲。学完之后，你应当能够：

- 说清 Nyuzi 测试框架 `tests/test_harness.py` 的整体职责：它如何把一份源码「编译链接 → 加载运行 → 自校验输出」串成一条流水线。
- 理解 `build_program` 如何根据 `image_type` 选择不同的链接方式（裸机 / raw / 用户态），以及它如何决定是否链接 `libc`/`libos`。
- 掌握「双目标」模型：同一个测试函数被框架在 `verilator` 与 `emulator` 上各调用一次，从而用一份代码验证两套实现。
- 理解 `CHECK` / `CHECKN` 自校验机制的工作原理，包括「按出现顺序匹配、忽略中间内容」这一关键约定。
- 会用 `@test_harness.test` 装饰器与 `register_*` 系列函数把一个新测试注册进框架，并知道框架最终如何调度与汇报结果。

本讲只讲「框架本身」如何工作；具体的随机测试、协同仿真、单元测试策略留给后续 u15-l2、u15-l3。

## 2. 前置知识

在开始之前，你需要先建立以下几个直觉（它们都来自前置讲义，这里只做最简回顾）：

- **两套执行后端**（来自 u1-l2、u8-l1）：Nyuzi 有两个可执行程序用来跑同一个程序——`nyuzi_emulator` 是 C 写的功能级指令集模拟器，快但不周期精确；`nyuzi_vsim` 是 Verilator 把 SystemVerilog RTL 编译出的周期精确仿真器，慢但能验证真实微架构。两套实现跑同一套 ISA，因此可以用「同一份测试在两个目标上跑」来交叉验证。
- **程序如何加载与停机**（来自 u1-l4、u9-l1）：裸机程序的入口是 `crt0.S` 的 `_start`，程序被 `elf2hex` 转成 hex 内存镜像后从地址 0 启动；`printf` 最终经 UART 写到模拟器/仿真器的标准输出；程序结束时向控制寄存器 `CR_SUSPEND_THREAD` 写 -1 停机。本讲中你会看到 `run_program` 如何把这些细节封装起来。
- **Python 基础**：装饰器、列表推导、`subprocess`、正则表达式（`re`）。本讲的测试框架全部用 Python 编写。
- **「测试」在本项目里的含义**：每个测试是一个 Python 函数 `func(name, target)`，它负责编译、运行、校验，失败时抛出 `TestException`。框架本身不关心你测什么，只负责「找到这个函数、按目标依次调用它、捕获异常、打印 PASS/FAIL」。

一个贯穿全讲的核心思想是：**框架把「编译/运行/校验」三件事解耦成可组合的积木**。你可以全部手写（自己调 `build_program` + `run_program` + `check_result`），也可以用框架提供的「通用测试」一行注册（`register_generic_test`）。理解这种「积木式」设计，是读懂本讲源码的钥匙。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [tests/test_harness.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py) | 测试框架本体，提供编译、运行、校验、注册、调度全部能力，被各子目录的 `runtest.py` 导入。 |
| [tests/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md) | 测试目录说明：如何运行、如何加测试、五类测试的总体策略。 |
| [tests/core/isa/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/runtest.py) | 最简注册示例：`find_files` + `register_generic_assembly_tests` + `execute_tests` 三行。 |
| [tests/libc/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/libc/runtest.py) | 混合示例：同时用 `@test` 装饰器注册特殊测试、用 `register_tests` 注册通用 CHECK 测试。 |
| [tests/fail/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/fail/runtest.py) | 「测试框架自身」的测试：用一堆注定失败的用例验证框架确实会报错。 |
| [tests/fail/check.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/fail/check.c) | 验证 `CHECK` 必须按顺序匹配（一个故意让顺序错乱而失败的例子）。 |
| [tests/fail/checkn.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/fail/checkn.c) | 验证 `CHECKN`（断言「不应出现」）确实会因出现而失败。 |

辅助文件（框架引用，但本讲不展开）：构建期生成的 `tests/tests.conf`（提供工具路径）、`tests/one-segment.ld`（raw 镜像的链接脚本）、`tests/asm_macros.h`（汇编测试共享的宏）。

## 4. 核心概念与源码讲解

本讲按「编译链接 → 双目标运行 → CHECK 校验 → 注册与调度」四个最小模块拆解，正好对应一条测试从「源文件」到「PASS/FAIL」的完整生命周期。

### 4.1 编译链接：build_program

#### 4.1.1 概念说明

一个 Nyuzi 测试的输入通常是一个 `.c`/`.cpp`/`.s`/`.S` 源文件，但模拟器和仿真器并不能直接吃源文件——它们只能加载 hex 内存镜像（参见 u1-l4）。因此测试框架的第一项职责，就是把源文件编译并链接成可加载的镜像。

`build_program` 就是这个「源文件 → 可加载镜像」的封装。它的关键设计有三点：

1. **根据文件后缀自动决定是否链接库**：只要源文件里有 `.c` 或 `.cpp`，就自动链入 `libc.a` 与 `libos`；纯汇编文件则只汇编不链库。
2. **用 `image_type` 区分三种运行场景**：裸机（`bare-metal`，最常见，ELF 链接后转 hex）、原始二进制（`raw`，链接在地址 0、无头）、用户态（`user`，链接到 `0x1000`、链内核版 libos，产物是 ELF 而非 hex）。
3. **编译失败抛 `TestException`**：把编译器输出原样塞进异常信息，方便定位。

#### 4.1.2 核心流程

`build_program(source_files, image_type, opt_level, cflags)` 的执行流程：

```text
1. 在 WORK_DIR 下确定输出名 program.elf
2. 组装 clang 命令行：clang -o program.elf -w <opt_level> -I <TEST_DIR> [<cflags>]
3. 按 image_type 追加链接选项：
     raw  -> -Wl,--script,one-segment.ld,--oformat,binary   （链接成地址 0 的裸二进制）
     user -> -Wl,--image-base=0x1000                         （用户态加载基址）
     bare-metal -> （不加特殊链接选项，走默认 ELF）
4. 追加源文件
5. 若含 .c/.cpp：追加 libc/libos 的头文件路径与静态库
     user       -> libos-kern.a
     否则        -> libos-bare.a
6. 调 clang 编译；失败则抛 TestException
7. 产出镜像：
     raw        -> dump_hex 把二进制按每行 4 字节编码成 program.hex
     bare-metal -> elf2hex 把 ELF 转 program.hex
     user       -> 直接返回 program.elf（由内核加载，无需转 hex）
8. 返回镜像路径
```

注意第 5 步的取舍：`libc` 是平台无关的（参见 u9-l1），但 `libos` 有两个变体——裸机版 `libos-bare.a` 直接碰硬件控制寄存器，内核版 `libos-kern.a` 走系统调用。`image_type == 'user'` 时选内核版，否则选裸机版。这就是「同一份 libc + 不同 libos 变体」复用到不同运行环境的关键接缝。

#### 4.1.3 源码精读

函数签名与文档（注意 `image_type` 只允许三种取值，靠 `assert` 守护）：

[tests/test_harness.py:79-109](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L79-L109) —— `build_program` 的定义、参数说明与 `image_type` 合法值断言。

命令行组装与按 `image_type` 追加链接脚本/基址：

[tests/test_harness.py:111-128](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L111-L128) —— 这段构造 `clang` 参数：`-w` 抑制警告、`-I TEST_DIR` 让汇编能 include `asm_macros.h`，并按 `raw`/`user` 分别追加 `one-segment.ld` 链接脚本与 `--image-base=0x1000`。

「有 C/C++ 才链库」的逻辑，以及裸机 vs 内核 libos 的分流：

[tests/test_harness.py:130-139](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L130-L139) —— `any(name.endswith(('.c', '.cpp')) ...)` 判定是否链库；`image_type == 'user'` 选 `libos-kern.a`，否则选 `libos-bare.a`。

镜像产出与异常包装：

[tests/test_harness.py:141-156](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L141-L156) —— `raw` 走 `dump_hex`、`bare-metal` 走 `elf2hex`、`user` 直接返回 ELF；`CalledProcessError` 被翻译成带完整编译输出的 `TestException`。

`raw` 镜像的 hex 编码方式（每行 4 字节）：

[tests/test_harness.py:743-768](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L743-L768) —— `dump_hex` 用 `binascii.hexlify` 把二进制按字编码，与 `$readmemh` 可读的 hex 格式一致。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `build_program` 对不同 `image_type` 与不同后缀的分流。

**操作步骤**：

1. 确认你已按 u1-l2 完成构建（`cmake . && make`），`tests/tests.conf` 已由构建系统生成（见 [tests/test_harness.py:39-51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L39-L51) 里读取该文件的逻辑）。
2. 进入 `tests` 目录，启动一个 Python 交互 shell：

   ```bash
   cd tests
   python3 -c "import test_harness; print(test_harness.COMPILER_BIN_DIR, test_harness.LIB_DIR)"
   ```

   预期打印出工具链与库目录的绝对路径，说明配置加载成功。
3. 手动调用 `build_program` 编译一个已有的 C 测试，观察产物：

   ```bash
   python3 -c "import test_harness; print(test_harness.build_program(['fail/check.c']))"
   ```

**需要观察的现象**：`WORK_DIR` 下出现 `program.elf` 与 `program.hex` 两个文件；命令打印出 `program.hex` 的路径。

**预期结果**：因为 `check.c` 是 `.c`，框架链入了 `libc.a` 与 `libos-bare.a`；产物是 hex。若把 `check.c` 换成一个纯汇编 `.s` 文件（如 `tests/core/isa/` 下任意一个），则不会链库。

> 若环境未完整安装工具链，命令会在第 3 步抛 `TestException: Compilation failed: ...`——这恰好验证了框架对编译错误的处理路径，属「待本地验证」范畴。

#### 4.1.5 小练习与答案

**练习 1**：为什么纯汇编测试（`.s`）不需要、也不会链入 `libc`/`libos`？

**参考答案**：`build_program` 用 `any(name.endswith(('.c', '.cpp')) ...)` 判定是否链库（[tests/test_harness.py:130](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L130)）。纯汇编后缀不命中，故只 assemble 不 link 库；汇编测试通常自己用 `load_32`/`store_32` 直接操作内存，不需要 C 运行时。

**练习 2**：`image_type='user'` 的产物与 `bare-metal` 有何不同？为什么？

**参考答案**：`user` 返回 `program.elf`（不转 hex），并链 `libos-kern.a`、链接基址 `0x1000`；`bare-metal` 返回 `program.hex`（经 `elf2hex`）并链 `libos-bare.a`。原因是用户态程序要由内核加载运行（见 u12-l1），内核负责把 ELF 装入独立地址空间，所以测试拿到 ELF 即可；裸机程序由模拟器/仿真器直接加载 hex 镜像。

---

### 4.2 双目标：run_program 与 verilator/emulator 分流

#### 4.2.1 概念说明

「双目标」是本框架最重要的设计：**同一个测试要在 `verilator` 与 `emulator` 上各跑一遍**。前者周期精确、能验证微架构（缓存、流水线、并发）；后者功能级、快、能当参考模型（详见 u8-l1、u8-l3 协同仿真）。两套实现必须对同一份程序给出相同的可观察输出，否则就说明其中之一有 bug。

`run_program(executable, target, ...)` 就是这个「按目标分流运行」的封装。它根据 `target` 组装完全不同的命令行：

- `emulator`：直接调 `nyuzi_emulator`，默认开 `-a`（线程调度随机化）。
- `verilator`：调 `nyuzi_vsim`，用 `+bin=` 指定镜像，注入随机复位与随机种子，并额外检查输出里是否含 `***HALTED***`（正常停机标志）。
- `fpga`：经 `serial_boot` 把镜像下载到真板（实验性，见 u14-l3）。

无论哪个目标，最终都返回「程序写到虚拟串口的标准输出」这个字符串——这是后续 `CHECK` 校验的输入。

#### 4.2.2 核心流程

`run_program` 的分流逻辑：

```text
target == 'emulator':
    args = [nyuzi_emulator, '-a', [-b block_device], [-d dump], executable]
    output = run_test_with_timeout(args, timeout)        # 捕获 stdout，超时即抛异常

target == 'verilator':
    random_seed = random.randint(0, 0xffffffff)          # 每次随机，覆盖复位态
    args = [nyuzi_vsim, +bin=executable,
            +verilator+rand+reset+2, +verilator+seed=<seed>,
            [+block=], [+memdumpfile/base/len], [+trace], [+profile=]]
    output = run_test_with_timeout(args, timeout)
    if '***HALTED***' not in output:                      # 没正常停机 = 崩溃/跑飞
        raise TestException('Program did not halt normally')

target == 'fpga':
    需环境变量 SERIAL_PORT；先 reset_fpga()，再 serial_boot <port> <exe>

return output
```

两个细节值得记住：①`run_test_with_timeout` 用 `subprocess.Popen` + `communicate(timeout=...)`，超时就 `kill()` 并抛 `TestException('Test timed out')`，非零退出码也抛异常（[tests/test_harness.py:191-222](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L191-L222)）；②`TerminalStateRestorer` 会保存/恢复 POSIX 终端属性，因为模拟器会关本地回显，崩溃时可能把终端搞坏（[tests/test_harness.py:168-188](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L168-L188)）。

#### 4.2.3 源码精读

两个目标可执行文件的路径常量（来自构建期 `tests.conf`）：

[tests/test_harness.py:53-57](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L53-L57) —— `VSIM_PATH`/`EMULATOR_PATH` 与 `ALL_TARGETS`/`DEFAULT_TARGETS` 都定义为 `['verilator', 'emulator']`。

emulator 分支（注意默认 `-a` 随机化线程调度）：

[tests/test_harness.py:278-291](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L278-L291) —— 组装并运行 emulator，支持块设备与内存转储。

verilator 分支（随机种子 + `***HALTED***` 检查）：

[tests/test_harness.py:292-327](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L292-L327) —— 这段是周期精确目标的核心：`+verilator+rand+reset+2` 与随机 seed 让每次仿真从不同状态出发，提高覆盖率；运行后强制校验 `***HALTED***`。

fpga 分支与未知目标保护：

[tests/test_harness.py:328-353](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L328-L353) —— fpga 需要 `SERIAL_PORT` 环境变量，且不支持 dump/flush_l2；其余 target 抛 `Unknown execution target`。

`run_test_with_timeout` 的超时与退出码处理：

[tests/test_harness.py:191-222](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L191-L222) —— `Popen` + `communicate(timeout=...)`，`TimeoutExpired` 时 `kill()` 后抛异常；`process.poll()` 非零（崩溃）也抛异常。

#### 4.2.4 代码实践

**实践目标**：直观对比同一程序在两个目标上的运行方式与耗时差异。

**操作步骤**：

1. 在 `tests` 目录下，用一个现成 hex 分别跑两个目标：

   ```bash
   cd tests
   python3 -c "
   import test_harness
   hexf = test_harness.build_program(['fail/checkn.c'])
   for t in ['emulator', 'verilator']:
       print('=== target:', t)
       print(repr(test_harness.run_program(hexf, t, timeout=30)[:80]))
   "
   ```

2. 用 `--debug` 走一次正式测试，观察框架打印的真实命令行（`DEBUG` 标志在 [tests/test_harness.py:67-76](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L67-L76) 解析）：

   ```bash
   cd tests/core/isa && ./runtest.py --debug <某个测试名> --target verilator
   ```

**需要观察的现象**：步骤 1 中 `emulator` 几乎瞬间返回；`verilator` 明显更慢（要启动 Verilator 编译出的模型）。步骤 2 会打印形如 `running verilator with args [..., '+bin=...', '+verilator+seed+...']` 的命令行。

**预期结果**：两个目标对同一个程序的可观察输出（串口打印）应当一致；只有运行时长与命令行不同。verilator 若没有输出 `***HALTED***`，`run_program` 会抛 `Program did not halt normally`。

> verilator 首次运行较慢；若未构建硬件模型，此步骤属「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `run_program` 只对 `verilator` 目标检查 `***HALTED***`，而 `emulator` 目标不检查？

**参考答案**：模拟器程序结束时写 `CR_SUSPEND_THREAD` 停机、进程自然退出（参见 u1-l4、u8-l1），`run_test_with_timeout` 通过 `process.poll()` 的零退出码即可判定正常结束。而 Verilator 仿真模型即使目标程序停了，仿真进程本身不会自动结束，需要靠打印 `***HALTED***` 这个 testbench 约定的标志来确认程序确实正常停机而非跑飞，故单独检查（[tests/test_harness.py:326-327](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L326-L327)）。

**练习 2**：`run_program` 默认给 emulator 加了 `-a`（线程调度随机化）。这会对测试带来什么好处？

**参考答案**：随机化硬件线程的调度顺序，能在多次运行中覆盖不同的线程交错，增加暴露数据竞争与并发 bug 的概率。配合 verilator 的随机 seed，双目标都能获得一定的随机覆盖率。

---

### 4.3 CHECK 校验：check_result

#### 4.3.1 概念说明

程序跑出来还不够，还要判断「结果对不对」。最朴素的做法是在 Python 里手写一堆 `assert 'xxx' in output`，但这样把期望值和源码分离了，维护痛苦。Nyuzi 借鉴 LLVM FileCheck 的思路，发明了**把期望输出直接写进源码注释**的方式：

- 在源文件里写 `// CHECK: <模式>`，表示「程序输出中应当出现这个模式」。
- 写 `// CHECKN: <模式>`（N 表示 Not），表示「程序输出中**不应**出现这个模式」。
- 模式是**正则表达式**，不是纯字符串。
- 多条 `CHECK` 之间必须**按出现顺序**匹配，但**允许中间有任意额外内容**。

这把「期望」和「产生期望的代码」放在同一行，读源码就能同时看到「我打印了什么」和「我期望什么」，极大地降低了测试维护成本。

#### 4.3.2 核心流程

`check_result(source_file, program_output)` 的工作流程：

```text
output_offset = 0                       # 在 program_output 中已匹配到的位置游标
found_check_lines = False
for line in source_file:                # 逐行扫描源文件
    if line 含 'CHECK: ':
        expected = CHECK: 之后的正则
        用 regexp.search(program_output, output_offset) 从游标处向后找
        找到 -> output_offset = 匹配末尾（推动游标，保证后续 CHECK 只能出现在更后面）
        没找到 -> 抛 TestException（并打印从游标处开始的剩余输出，方便定位）
    elif line 含 'CHECKN: ':
        nexpected = CHECKN: 之后的正则
        用 regexp.search(program_output, output_offset) 查找
        找到 -> 抛 TestException（不该出现的串居然出现了）
        没找到 -> 通过（继续保持游标不变）
if 一次 CHECK/CHECKN 都没遇到:
    抛 TestException('no lines with CHECK: were found')   # 防止「忘了写 CHECK」的空测试
```

这里最关键的算法点是 **`output_offset` 游标**：每条 `CHECK` 匹配成功后，游标推进到匹配末尾，所以下一条 `CHECK` 只能在「更靠后」的位置匹配。这保证了几条 `CHECK` 反映的是输出中的**相对顺序**而非绝对连续。两条 `CHECK` 之间可以有任意噪声（时间戳、调试日志等）都不会影响判定。

`CHECKN` 则反过来——它在当前游标之后的**任意位置**出现就算失败。注意 `CHECKN` 不推进游标。

一个易错点：因为模式是正则，源码里若出现 `0x12345678` 这种是安全的（都是字母数字），但若期望里含 `.`、`*`、`(`、`[` 等元字符，会被当作正则语法。本项目的 `CHECK` 大多是简单十六进制/字符串，规避了这个问题。

#### 4.3.3 源码精读

两个前缀常量（注意都带末尾空格）：

[tests/test_harness.py:672-673](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L672-L673) —— `CHECK_PREFIX = 'CHECK: '`、`CHECKN_PREFIX = 'CHECKN: '`。

`check_result` 主体（游标推进与有序匹配）：

[tests/test_harness.py:698-719](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L698-L719) —— `chkoffs = line.find(CHECK_PREFIX)` 用 `find`（不是 `startswith`），所以 `CHECK` 可以藏在 `// ` 注释里任意位置；`regexp.search(program_output, output_offset)` 从游标处向后搜；命中后 `output_offset = got.end()` 推进游标。

`CHECKN` 分支（出现即失败，不推进游标）：

[tests/test_harness.py:721-735](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L721-L735) —— `CHECKN` 同样从 `output_offset` 搜索，但语义相反：找到了就抛异常。

「没有任何 CHECK 行」保护：

[tests/test_harness.py:739-740](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L739-L740) —— 若整个源文件没有一条 `CHECK`/`CHECKN`，直接抛错，防止「空测试」永远静默通过。

一个故意失败的样例，正好演示「顺序约束」。`check.c` 先打印 `foo` 再打印 `bar`，但 `CHECK` 顺序写反了：

[tests/fail/check.c:22-27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/fail/check.c#L22-L27) —— 第一条 `CHECK: bar` 会匹配到输出里的 `bar` 并把游标推到 `bar` 之后；第二条 `CHECK: foo` 再从 `bar` 之后找 `foo`，但 `foo` 在 `bar` 之前已经打印过、后面没有了，于是失败。这个测试**故意要失败**，用来证明框架确实强制顺序。

`CHECKN` 样例：程序打印了 `bar`，而 `CHECKN: bar` 断言它不该出现，于是失败：

[tests/fail/checkn.c:21-25](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/fail/checkn.c#L21-L25) —— 验证 `CHECKN` 的「出现即失败」语义。

#### 4.3.4 代码实践

**实践目标**：用一个最小例子亲历 `CHECK` 的「有序匹配」与 `CHECKN` 的「负向断言」，并观察失败时的错误信息。

**操作步骤**：

1. 在 `tests/work/`（或任意临时目录）新建 `demo_check.c`（**示例代码**，非项目原有文件）：

   ```c
   #include <stdio.h>
   int main(void) {
       printf("step1 0x%08x\n", 0x11);   // CHECK: step1 0x00000011
       printf("noise line\n");
       printf("step2 0x%08x\n", 0x22);   // CHECK: step2 0x00000022
       printf("done\n");                 // CHECKN: error
       return 0;
   }
   ```

2. 用框架编译、运行、校验一条龙：

   ```bash
   cd tests
   python3 -c "
   import test_harness
   src = 'work/demo_check.c'
   hexf = test_harness.build_program([src])
   out  = test_harness.run_program(hexf, 'emulator')
   print('--- program output ---'); print(out)
   test_harness.check_result(src, out)
   print('CHECK PASSED')
   "
   ```

3. 故意把第一条 `CHECK` 改成 `// CHECK: step2`（与第二条互换语义），重新运行步骤 2，观察框架报错。

**需要观察的现象**：步骤 2 打印 `CHECK PASSED`；尽管输出里有 `noise line` 这条 `CHECK` 未提及的噪声行，校验仍然通过（证明「忽略中间内容」）。步骤 3 会抛 `TestException`，信息形如 `FAIL: line N expected string ... was not found`，并附上「从游标处开始的剩余输出」。

**预期结果**：`CHECK` 反映相对顺序而非绝对连续；`CHECKN: error` 因输出不含 `error` 而通过；一旦把顺序写错，游标机制会让后一条 `CHECK` 找不到目标而失败。

> 若无工具链，步骤 2 会在 `build_program` 阶段失败——此时可只读 `tests/fail/check.c` 与 `tests/fail/checkn.c`，结合上文推理出它们为何被设计成「注定失败」，属「源码阅读型实践」。

#### 4.3.5 小练习与答案

**练习 1**：假如源文件里写 `// CHECK: 0x123`，而程序输出是 `val 0x123456`，这条 `CHECK` 会通过吗？为什么？

**参考答案**：会通过。`check_result` 用 `re.compile(expected).search(...)`（[tests/test_harness.py:711-712](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L711-L712)），`search` 是「在任意位置找子串匹配」，`0x123` 是 `0x123456` 的前缀子串，故命中。若想要求「整行精确等于」，需要用 `^...$` 锚点，但本项目一般不这么做。

**练习 2**：`check_result` 为什么要检测「一个 CHECK 都没有」并主动报错？

**参考答案**：防止「忘了写期望」的测试变成永远静默通过的假绿（[tests/test_harness.py:739-740](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L739-L740)）。一个不校验任何输出的测试没有任何价值，框架宁可让它显式失败，也不让它误报成功。

---

### 4.4 注册机制：装饰器、register_* 与 execute_tests

#### 4.4.1 概念说明

前面三个模块给出了「积木」：怎么编译、怎么运行、怎么校验。但框架还需要知道**有哪些测试**、**每个测试在哪些目标上跑**、**按什么顺序调度**、**怎么汇报结果**。这就是「注册机制」要解决的问题。

Nyuzi 框架用一个全局列表 `registered_tests` 存放所有测试，每个元素是三元组 `(func, name, targets)`：

- `func(name, target)`：测试函数本体；
- `name`：测试名（用于命令行筛选与结果打印）；
- `targets`：该测试支持的目标列表，如 `['verilator', 'emulator']`。

注册有三种风格，从简到繁：

1. **装饰器风格** `@test_harness.test`：最常用，把紧随其后的函数自动注册，函数名即测试名。
2. **通用测试风格** `register_generic_test` / `register_generic_assembly_tests`：无需手写函数，框架提供内置处理器，自动「编译 → 运行 → CHECK/PASS 校验」。
3. **手动风格** `register_tests(func, names, targets)`：完全自定义，用一份函数处理多个测试名。

最后由 `execute_tests()` 在每个 `runtest.py` 末尾统一调度：解析命令行（`--target`/`--debug`/`--list`/测试名）、按目标逐个调用、捕获 `TestException` 与普通异常、统计并打印 PASS/FAIL、失败时以非零码退出。

#### 4.4.2 核心流程

注册与调度的整体流程：

```text
# —— 注册阶段（runtest.py 被导入时执行）——
@test_harness.test                       # 不带参数：func 本身作为 param，注册到 ALL_TARGETS
def my_test(name, target): ...

@test_harness.test(['emulator'])         # 带参数：返回 register_func，注册到指定目标
def fast_only(name, target): ...

test_harness.register_generic_test(['a.c','b.c'])           # 内置 _run_generic_test 处理器
test_harness.register_generic_assembly_tests(find_files(('.s',)))  # 内置汇编处理器

# registered_tests = [(my_test,'my_test',['verilator','emulator']),
#                     (fast_only,'fast_only',['emulator']),
#                     (_run_generic_test,'a.c',[...]), ...]

# —— 调度阶段（execute_tests）——
1. 若 --list：打印 所有 (name, targets) 后返回
2. 决定 targets_to_run：--target 指定值，否则 DEFAULT_TARGETS
3. 决定 tests_to_run：命令行给了名字就按名字筛，否则全跑
4. for (func, name, targets) in tests_to_run:
       for target in targets:
           if target not in targets_to_run: continue      # 跳过本次不跑的目标
           清空并重建 WORK_DIR
           try: func(name, target); 打印 PASS
           except TestException: 打印 FAIL，记入 failing_tests
           except Exception: 打印 FAIL，附带完整 traceback
5. 打印 'N/M tests failed'；若有失败，sys.exit(1)
```

两个设计要点：①**每个 (测试, 目标) 组合是一个独立调度单元**，所以一个支持双目标的测试会被调度两次，分别打印 `name(emulator)` 与 `name(verilator)`；②每次调度前**清空重建 `WORK_DIR`**（[tests/test_harness.py:640-642](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L640-L642)），保证测试间隔离，避免上一个测试的 `program.hex` 污染下一个。

#### 4.4.3 源码精读

装饰器 `test`：巧妙地用一个函数同时支持「带括号」与「不带括号」两种用法：

[tests/test_harness.py:526-546](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L526-L546) —— 当 `@test` 不带括号时，被装饰函数本身被当作 `param` 传入（`callable(param)` 为真），直接注册到 `ALL_TARGETS`；当 `@test([...])` 带括号时，`param` 是目标列表，返回内层 `register_func` 等待真正接收函数。两种用法最终都调 `register_tests`。

`register_tests`：把三元组追加进全局列表：

[tests/test_harness.py:498-523](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L498-L523) —— `targets` 为空时回退到 `ALL_TARGETS`；可多次调用累加。

通用测试的内置处理器 `_run_generic_test`（编译→运行→CHECK 三行）：

[tests/test_harness.py:786-808](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L786-L808) —— 这就是把 4.1/4.2/4.3 三个积木粘合在一起的「默认实现」：`build_program([name])` → `run_program(hex_file, target)` → `check_result(name, result)`。

汇编测试的内置处理器（用 PASS/FAIL 字符串而非 CHECK）：

[tests/test_harness.py:835-839](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L835-L839) —— `_run_generic_assembly_test` 不用 `check_result`，而是要求程序自己打印 `PASS`、且不含 `FAIL`。这是汇编测试的常见约定（汇编里不便写 `CHECK` 注释）。

`execute_tests` 的过滤与调度：

[tests/test_harness.py:604-627](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L604-L627) —— `--list` 分支、`--target` 分支、按名字筛选分支；命令行里给定的名字若不存在会 `sys.exit(1)`。

主调度循环（双目标遍历、隔离、异常捕获）：

[tests/test_harness.py:632-659](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L632-L659) —— 嵌套两层循环（测试 × 目标）；`target not in targets_to_run` 跳过本次不跑的目标；`TerminalStateRestorer` 包裹实际调用；`TestException` 与普通 `Exception` 都被捕获并记为 FAIL，但后者额外附带 `traceback`。

结果汇报与非零退出：

[tests/test_harness.py:661-670](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L661-L670) —— 打印每个失败测试的名字与输出；只要有失败就 `sys.exit(1)`，这正是 CI 能据此判定成败的接口。

最简真实示例：`tests/core/isa/runtest.py` 三行搞定一整套汇编测试：

[tests/core/isa/runtest.py:23-25](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/runtest.py#L23-L25) —— `find_files` 抓取当前目录所有 `.s/.S`，`register_generic_assembly_tests` 一次性全部注册到 `['emulator','verilator','fpga']`，最后 `execute_tests()` 调度。

混合示例：`tests/libc/runtest.py` 同时用装饰器与手动注册：

[tests/libc/runtest.py:27-56](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/libc/runtest.py#L27-L56) —— `filesystem` 测试用 `@test_harness.test(['emulator','fpga'])` 注册（需要块设备，故自定义逻辑）；其余 `.c` 走 `register_tests(run_test, ...)` 配合 `check_result`。

「测试框架自身」的测试 `tests/fail/runtest.py`：一堆注定失败的用例，用来证明框架真的会报错：

[tests/fail/runtest.py:30-84](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/fail/runtest.py#L30-L84) —— `timeout`/`assemble_error`/`files_not_equal`/`exception`/各 `assert_*` 故意失败；`check.c`/`checkn.c` 经 `register_generic_test` 注册。这个目录的测试**期望全部 FAIL**，故顶层 Makefile 不会跑它（见 tests/README.md 的排除表）。

#### 4.4.4 代码实践

**实践目标**：亲手写一个新测试，用 `@test_harness.test` 装饰器注册，在 `emulator` 与 `verilator` 两个目标上各跑一次，并用 `CHECK` 注释自校验输出。这是本讲规格指定的核心实践。

**操作步骤**：

1. 新建一个测试目录 `tests/work/myfirst/`（**示例代码**，仅供练习；正式测试应放在 `tests/` 下合适的子目录并配 `runtest.py`）。放入两件东西。

   先写被测程序 `myfirst.c`（期望输出写在注释里）：

   ```c
   #include <stdio.h>
   int main(void) {
       int a = 6, b = 7;
       printf("sum %d\n", a + b);        // CHECK: sum 13
       printf("prod %d\n", a * b);       // CHECK: prod 42
       return 0;
   }
   ```

   再写驱动脚本 `runtest.py`（仿照 `tests/libc/runtest.py` 的结构）：

   ```python
   #!/usr/bin/env python3
   import os, sys
   sys.path.insert(0, '../..')          # 让 import test_harness 生效
   import test_harness

   @test_harness.test                    # 不带括号 = 注册到 ALL_TARGETS(verilator+emulator)
   def myfirst(name, target):
       hex_file = test_harness.build_program([name + '.c'])
       result = test_harness.run_program(hex_file, target)
       test_harness.check_result(name + '.c', result)

   test_harness.execute_tests()
   ```

2. 运行它，默认在两个目标上各跑一次：

   ```bash
   cd tests/work/myfirst && ./runtest.py
   ```

3. 只在 emulator 上跑、并开 debug 看命令行：

   ```bash
   ./runtest.py --target emulator --debug
   ```

4. 列出已注册测试：

   ```bash
   ./runtest.py --list
   ```

**需要观察的现象**：步骤 2 会打印两行结果，分别形如 `myfirst(emulator)  ... PASS` 与 `myfirst(verilator) ... PASS`（两目标各一次，证明「双目标」）。步骤 3 只打印一行 `myfirst(emulator)`，并额外打印 `running emulator with args [...]`。步骤 4 打印 `myfirst: emulator, verilator`。

**预期结果**：装饰器把 `myfirst` 注册到 `['verilator','emulator']`；`execute_tests` 默认对每个目标各调度一次；`check_result` 用两条 `CHECK` 验证 `sum 13` 与 `prod 42` 有序出现。若把 `prod 42` 改成错误的 `prod 99`，对应目标会报 FAIL 且整体以非零码退出。

> 若 verilator 模型未构建，步骤 2 的 verilator 分支会失败或超时——这正好让你看到框架如何把单个目标的失败计入 `failing_tests` 并最终 `sys.exit(1)`。可先用 `--target emulator` 验证功能正确性。

#### 4.4.5 小练习与答案

**练习 1**：`@test_harness.test` 与 `@test_harness.test(['emulator'])` 在源码里走的是同一段逻辑吗？它是如何区分这两种用法的？

**参考答案**：是同一段逻辑（[tests/test_harness.py:526-546](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L526-L546)）。区分手段是判断 `param` 是否 `callable`：不带括号时 Python 直接把函数作为 `param` 传入，`callable(param)` 为真，注册到 `ALL_TARGETS`；带括号时 `param` 是目标列表（不可调用），函数返回内层 `register_func`，后者在收到真正函数时才注册到指定目标。

**练习 2**：为什么每个测试被调度前都要 `shutil.rmtree(WORK_DIR)` 再重建？不重建会有什么后果？

**参考答案**：为了测试间隔离（[tests/test_harness.py:640-642](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L640-L642)）。所有测试都把产物写到同一个 `WORK_DIR`（`program.elf`/`program.hex` 等）。若不清理，前一个测试的 `program.hex` 可能残留，导致后一个测试在意外情况下加载到旧镜像，给出误导性的「通过」。重建目录保证每次都从干净状态开始。

**练习 3**：如果一个测试同时被 `@test_harness.test` 与随后的 `@test_harness.disable` 修饰，会发生什么？

**参考答案**：`disable` 会检查 `registered_tests[-1][0] == func`（即最近注册的确实是这个函数），然后 `del registered_tests[-1]` 把它删掉（[tests/test_harness.py:549-560](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L549-L560)）。效果是该测试被临时禁用、不参与调度，常用于暂时跳过不稳定用例。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「从零添加一个正式可运行测试」的完整任务。建议放在 `tests/whole-program/` 旁边新建一个独立小目录，因为它就是一个「整机程序」式测试。

**任务**：编写一个程序，计算斐波那契数列前若干项并用 `printf` 输出，用 `CHECK` 注释断言关键项的值；再用本讲学到的注册机制让它同时跑在 `emulator` 与 `verilator` 上。

**建议步骤**：

1. **写被测程序 `fib.c`**（**示例代码**）。期望值用注释内联：

   ```c
   #include <stdio.h>
   int fib(int n) {
       return n < 2 ? n : fib(n - 1) + fib(n - 2);
   }
   int main(void) {
       printf("fib10 %d\n", fib(10));   // CHECK: fib10 55
       printf("fib15 %d\n", fib(15));   // CHECK: fib15 610
       printf("done\n");                // CHECKN: error
       return 0;
   }
   ```

   先用宿主 `cc` 编译运行一次，确认 `fib(10)=55`、`fib(15)=610`，把正确的期望填进 `CHECK`。

2. **写 `runtest.py`**。要求：用 `@test_harness.test` 注册一个 `fib` 测试（双目标），函数体内依次调用 `build_program` → `run_program` → `check_result`。参考 [tests/libc/runtest.py:46-49](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/libc/runtest.py#L46-L49) 的 `run_test` 写法。

3. **跑通两个目标**：`./runtest.py` 应看到 `fib(emulator) PASS` 与 `fib(verilator) PASS`。

4. **故意制造失败并观察报告**：把 `fib10 55` 改成 `fib10 54`，重新运行，确认框架打印 FAIL、给出 `FAIL: line N expected string fib10 55 was not found` 并以非零码退出。

5. **（选做）切换到「通用测试」风格**：删掉自定义函数，改用 `test_harness.register_generic_test(['fib.c'])` 一行注册，体会「不写函数也能跑」的便利（其内部就是 4.4.3 的 `_run_generic_test`）。

**检验标准**：能解释清楚「一条 `CHECK` 对应一次正则 `search`、游标只前进」、「`(func, name, targets)` 三元组如何被双目标展开成两次调度」、「编译失败如何被翻译成 `TestException`」三件事，本讲就过关了。

## 6. 本讲小结

- Nyuzi 测试框架 `tests/test_harness.py` 把测试生命周期拆成三块积木：`build_program`（编译链接）、`run_program`（按目标运行）、`check_result`（自校验输出），再由注册与调度机制把它们组合执行。
- `build_program` 按源文件后缀决定是否链 `libc`/`libos`，按 `image_type`（`bare-metal`/`raw`/`user`）选择链接脚本与镜像产物（hex 或 elf），编译失败抛 `TestException`。
- 「双目标」是核心设计：`run_program` 对 `emulator`（快、功能级）与 `verilator`（慢、周期精确、需检查 `***HALTED***`）组装不同命令行，但统一返回串口输出；同一测试因此能在两套实现上交叉验证。
- `CHECK` / `CHECKN` 把期望输出以正则形式写进源码注释；`check_result` 用一个只前进的 `output_offset` 游标实现「按顺序匹配、忽略中间噪声」，`CHECKN` 断言「不应出现」，且会检测「一个 CHECK 都没有」以防空测试。
- 注册有三风格：`@test` 装饰器（最常用，靠 `callable(param)` 同时支持带/不带括号）、`register_generic_test` / `register_generic_assembly_tests`（内置处理器，免写函数）、`register_tests`（全自定义）；所有测试进入全局 `registered_tests` 三元组列表。
- `execute_tests` 负责 `--list`/`--target`/按名筛选、按「测试 × 目标」双重循环调度、每轮清空 `WORK_DIR` 保隔离、捕获 `TestException` 与普通异常并打印 PASS/FAIL，有失败则 `sys.exit(1)` 供 CI 判定。

## 7. 下一步学习建议

本讲只讲了「框架本身」。要理解 Nyuzi 验证体系的全貌，建议继续：

- **u15-l2 随机测试生成与约束**：看 `tests/cosimulation/generate_random.py` 如何用约束随机生成海量汇编程序，把本讲的「双目标」升级为「硬件 vs 模拟器逐指令锁步比对」（u8-l3 协同仿真）。
- **u15-l3 单元测试与整机测试策略**：对比 `tests/unit`（模块级、周期精确、能看内部信号）、`tests/core/isa`（定向功能测试）、`tests/whole-program`（整机程序输出比对）三类测试如何互补，以及本讲的 `register_generic_assembly_tests` / `register_generic_test` / `register_render_test` 分别服务哪一类。
- **直接读源码**：在 `tests/` 下任选一个子目录的 `runtest.py`，对照本讲四个模块，画出它「注册了哪些测试、用哪种风格、跑哪些目标、靠什么校验」的表格；再读 `tests/README.md` 的「Test Approach」一节，把五类测试与本讲的框架函数对应起来。
- **想动手扩展**：尝试为某个还没被覆盖的指令或外设行为，按本讲第 5 节的流程新增一个 `CHECK` 式测试，并在 `emulator` 与 `verilator` 上都跑通——这是检验你是否真正掌握本讲的最佳方式。
