# Stub 注册与内建函数转义

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 cpudebug 在 CPU 域运行算子时，源码里那些「内建函数」（`Add`、`DataCopy`、`Exp`……）是怎么被绑定到一段可执行的 CPU 代码上的。
- 读懂 `StubInit` / `StubReg` 的注册流程：一个前缀数组 `g_regStubs`、一张格式表 `IntriFmtGet`、一次 `dlsym` 动态查找、一次 `IntriFunAdd` 入表，如何合力把 5149 个内建函数逐一挂进函数表。
- 理解 `dlsym(RTLD_DEFAULT, ...)` 的动态绑定原理，以及它在「正向绑定」与崩溃时的「逆向回溯」（`stub_backtrace.cpp`）中分别扮演的角色。
- 区分 `AscendC` / `cceprint` / `npuchk` 三类 stub 实现各自的职责，并能指出它们各自由哪个生成脚本产出。

本讲承接 [u3-l1 多核 fork 执行模型](u3-l1-fork-execution-model.md)：那里讲到 `RunKernelFunctionOnCpu` 在 fork 之前会调用一句 `AscendC::StubInit();`，本讲就把这一句彻底拆开。

## 2. 前置知识

阅读本讲前，最好已经了解：

- **内建函数（intrinsic）**：Ascend C 源码里 `Add(dst, src0, src1)`、`DataCopy(local, global, size)` 这类调用，对应 NPU 上的一条硬件向量/搬运指令。它们没有源码实现，靠编译器/工具链提供。
- **CPU 域孪生调试**：cpudebug 让同一份算子源码经 GCC 编译在 CPU 上跑通（见 [u2-l1](u2-l1-cpudebug-workflow.md)）。于是上面这些「没有实现的内建函数」必须在 CPU 上也有一份等价实现，否则链接不过。
- **动态链接 `dlsym`**：Linux 下 `<dlfcn.h>` 提供的接口，可以在程序运行时按「符号名字符串」查到一个全局函数的地址。
- **`fork` 执行模型**：每个核是一个子进程（见 [u3-l1](u3-l1-fork-execution-model.md)）。本讲的注册发生在父进程 fork **之前**，因此子进程天然继承这张已经建好的函数表。

一个关键直觉先放在这里：**cpudebug 不是为每个内建函数写死一段 CPU 实现，而是写了一张「符号名 → 函数指针」的二维表**。算子调用内建函数时，实际上是查表跳转。本讲讲的就是这张表是怎么被填满的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [cpudebug/include/stub_reg.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/stub_reg.h) | 对外声明 `StubReg` / `StubInit`，引入格式表与函数表两个头文件。 |
| [cpudebug/src/regfwk/stub_reg.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp) | **本讲主角**。定义前缀数组 `g_regStubs` 与注册主循环 `StubInit`。 |
| [cpudebug/include/intri_fun.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/intri_fun.h) | 定义内建函数类别枚举 `IntriTypeT`、函数指针类型 `PfIntriFun`、入表接口 `IntriFunAdd`。 |
| [cpudebug/include/intri_fmt.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/intri_fmt.h) | 定义单个内建函数的格式描述 `IntriFmt` 与取格式接口 `IntriFmtGet`。 |
| [cpudebug/src/regfwk/stub_backtrace.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_backtrace.cpp) | `dlsym` 的「逆过程」：用 `dladdr` / `dl_iterate_phdr` 把崩溃地址还原成源码行。 |
| [cpudebug/src/regfwk/stub_base.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_base.cpp) | 注册流程依赖的全局状态（`block_idx`、`g_kernelMode`、`GmAlloc` 等）与运行时支撑。 |
| [cpudebug/cmake/fun.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/fun.cmake) | 闭源生成脚本入口：`reg_funs_gen.py` 生成函数/格式表，`cce_stub.py` / `write_npuchk.py` 生成对应 stub。 |

> **开源/闭源边界提示**：`IntriFmtGet`（格式表）、`IntriFunAdd`（函数表）以及具体每个内建函数的 stub 实现，都不是仓库里手写的源码，而是构建时由 `model/scripts/` 下的 Python 脚本**生成**的 `intri_fmt.cc` / `intri_fun.cc` 等文件。本讲读的是「驱动这张表被填满」的开源代码（`stub_reg.cpp`），表本身是生成产物。

## 4. 核心概念与源码讲解

### 4.1 StubReg / StubInit 流程

#### 4.1.1 概念说明

回忆一下问题：算子源码里调了 `Add(...)`，但 `Add` 在 NPU 上是一条指令，CPU 上并没有这个名字的函数。cpudebug 的做法是——**不直接实现 `Add` 这个名字，而是维护一张二维函数表**：

- 表的**行**是「内建函数编号」`fid`（from *function id*），一共有 `INTRI_FMT_NUM = 5149` 行，覆盖所有内建函数及其不同数据类型的重载。
- 表的**列**是「实现类别」`type`，取自枚举 `IntriTypeT`，一共有 `INTRI_TYPE_MAX = 5` 列。
- 表格单元 `(fid, type)` 存放一个函数指针 `PfIntriFun`。

表格规模为：

\[
\text{表单元数} = \text{INTRI\_FMT\_NUM} \times \text{INTRI\_TYPE\_MAX} = 5149 \times 5
\]

`StubInit` 的工作就是把这张表**一次性填满**：遍历每一个 `fid`、每一个 `type`，用 `dlsym` 查到对应的 CPU 实现地址，写进 `(fid, type)` 单元。之后算子运行时调用 `Add`，就被引导到表里对应的函数指针去执行。

`StubReg` 则是一个「改列名」的口子：在 `StubInit` 之前调用 `StubReg(type, "新前缀")`，可以把某一列绑定的实现前缀换掉，从而换一套实现。

#### 4.1.2 核心流程

`StubInit` 的执行流程可以概括为：

```text
StubInit()                            # 由父进程在 fork 之前调用一次
 ├── 若已初始化(gStubInited) → 直接返回   # 幂等保护
 ├── 打开 stub_reg.log                 # 记录每个符号的绑定结果
 ├── for s in [0, INTRI_TYPE_MAX):     # 遍历 5 个类别（列）
 │     stub = g_regStubs[s]
 │     if stub == nullptr: continue   # USER1/USER2 默认空，跳过
 │     slen = strlen(stub)
 │     for i in [0, INTRI_FMT_NUM):   # 遍历 5149 个内建函数（行）
 │         fmt = IntriFmtGet(i)       # 取该内建函数的「符号格式」
 │         buf  = snprintf(fmt, slen, stub)   # 渲染出完整符号名
 │         fun  = dlsym(RTLD_DEFAULT, buf)    # 按名字查函数地址
 │         IntriFunAdd(i, s, fun)     # 写入 (fid=i, type=s) 单元
 │         写一行日志: stub: [buf] -> fun
 └── 关闭日志
```

整个函数的时间复杂度是 \(O(\text{INTRI\_TYPE\_MAX} \times \text{INTRI\_FMT\_NUM})\)，因为 `dlsym` 要做符号查找，这是一次「启动期开销」，只发生在 kernel 启动时、且因 `gStubInited` 守卫只跑一次。

#### 4.1.3 源码精读

注册的起点是 [u3-l1](u3-l1-fork-execution-model.md) 里 `RunKernelFunctionOnCpu` 的这一行，它在父进程分配完共享内存、打包完参数之后、`fork` 之前被调用：

[cpudebug/include/kern_fwk.h:L96-L96](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L96-L96) —— 在 fork 之前完成 stub 注册，子进程通过 `fork` 继承这张表。

「类别」枚举定义如下，注意它和后面三个前缀字符串是一一对应的：

[cpudebug/include/intri_fun.h:L23-L32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/intri_fun.h#L23-L32) —— `INTRI_TYPE_CPU=0` 对应功能实现，`INTRI_TYPE_NPU=1`、`INTRI_TYPE_CCE=2` 对应另外两类，`USER1/USER2` 留给二次开发；`PfIntriFun` 是一个可变参数函数指针 `uint64_t (*)(...)`，这样同一张表就能装下任意签名的内建函数。

前缀数组 `g_regStubs` 就是用下标 `0..2` 把这三类实现的前缀名填进去：

[cpudebug/src/regfwk/stub_reg.cpp:L25-L34](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp#L25-L34) —— 默认三类前缀 `"AscendC"` / `"cceprint"` / `"npuchk"`，下标分别等于 `INTRI_TYPE_CPU` / `INTRI_TYPE_NPU` / `INTRI_TYPE_CCE`；`StubReg` 只是改写某个下标的字符串。

主体 `StubInit` 的双层循环：

[cpudebug/src/regfwk/stub_reg.cpp:L36-L69](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp#L36-L69) —— 外层遍历类别（列），内层遍历 5149 个格式（行）；`gStubInited` 保证全局只执行一次；`stub_reg.log` 把每个 `符号名 -> 地址` 的绑定结果落盘，是排查「某个内建函数没绑上」的关键证据。

其中循环体最核心的三步：

[cpudebug/src/regfwk/stub_reg.cpp:L53-L61](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp#L53-L61) —— `IntriFmtGet(i)` 取格式、`snprintf_s` 渲染符号名、`dlsym` 查地址、`IntriFunAdd` 入表，再 `dprintf` 写日志。本讲 4.2 会专门拆 `dlsym`，4.3 会解释三个前缀的含义。

#### 4.1.4 代码实践

**实践目标**：验证 `StubInit` 是「一次性、幂等」的，并找到它落盘的日志。

1. 在 [stub_reg.cpp:L39-L43](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp#L39-L43) 找到 `static bool gStubInited` 守卫，确认第二次调用 `StubInit()` 会直接 `return`。
2. 在 [stub_reg.cpp:L45-L45](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp#L45-L45) 确认日志文件名为 `stub_reg.log`（相对当前工作目录，用 `O_CREAT|O_WRONLY|O_TRUNC` 每次覆盖）。
3. 按 [u1-l4](u1-l4-build-and-first-sample.md) 的方式跑一次 add 样例，结束后在**样例的可执行文件所在目录**下查找 `stub_reg.log`。

**需要观察的现象**：日志里应有形如 `AscendC: [<符号名>] -> 0x<地址>`、`cceprint: [...] -> ...`、`npuchk: [...] -> ...` 的三类条目，且每个内建函数对应 3 行（USER1/USER2 为空会被 `continue` 跳过，不会出现）。

**预期结果**：日志行数约为 \(3 \times 5149\) 条（三类前缀各一遍 5149 个格式），若某些符号在当前架构下不存在，其地址列为 `0x0` 或 `(nil)`——这属于「该架构未启用某内建函数」，不一定异常。

> **待本地验证**：`stub_reg.log` 的确切行数、以及其中 `-> (nil)` 的比例，依赖你编译时选择的产品架构（ascend910 / 910B1 / 950pr_9599 …），请以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `StubInit()` 的调用从 `fork` 之前移到每个子进程里（即移进 `pid == 0` 分支），会有什么后果？

> **参考答案**：功能上仍可工作，但每个子进程都会重复跑一遍 5149×5 次 `dlsym`，造成明显的启动期开销浪费；且 `gStubInited` 是进程级 `static` 变量，子进程各自独立、互不共享，无法靠它跨进程去重。这正是当前代码把它放在 fork **之前**、由父进程一次建表、子进程继承的原因。

**练习 2**：`StubReg(INTRI_TYPE_USER1, "myimpl")` 之后，`g_regStubs[3]` 的值是什么？`StubInit` 会如何处理它？

> **参考答案**：`g_regStubs[3]` 变为指向 `"myimpl"` 的指针。`StubInit` 的外层循环到 `s=3` 时不再 `continue`，会用前缀 `"myimpl"` 去 `dlsym` 查找并填入第 4 列（`INTRI_TYPE_USER1`）。若进程里没有任何符号以该前缀命名，则查到的地址为空，`(fid, USER1)` 单元填入空指针。

---

### 4.2 dlsym 动态绑定

#### 4.2.1 概念说明

`dlsym` 是 POSIX 动态链接库 `<dlfcn.h>` 提供的函数：

```cpp
void* dlsym(void* handle, const char* symbol);
```

它接收一个「句柄」和一个「符号名字符串」，返回该符号在内存中的地址。`stub_reg.cpp` 用的是特殊句柄 `RTLD_DEFAULT`，含义是：**在当前进程已加载的所有共享库（含主程序）的全局符号里**查找，按加载顺序取第一个匹配。

这正是 cpudebug 需要的能力：

- 算子可执行文件链接了 `libcpudebug.so`（功能实现）、以及 cceprint / npuchk 等几套 stub 实现。
- 这几套库里有成千上万个按命名约定生成的函数符号。
- cpudebug 不想在源码里手写 5149 个 `if (name == "Add") return Add_cpu;`，而是用一个统一约定：**符号名 = 前缀 + 内建函数后缀**，再用 `dlsym` 按名字取地址。

> **类比**：这就像把所有函数挂进一本按「名字」排序的电话簿，`dlsym` 是查号台，`g_regStubs` 决定查号时用哪个「姓氏」（前缀）。

#### 4.2.2 核心流程

「正向绑定」与「逆向回溯」是同一套动态链接机制的两次使用：

```text
正向（启动期，stub_reg.cpp）：
   字符串符号名  --dlsym-->  函数指针  --IntriFunAdd-->  函数表

逆向（崩溃期，stub_backtrace.cpp）：
   运行时 PC 地址 --dladdr-->  所在共享对象+偏移  --addr2line-->  源码文件:行号
```

正向用 `dlsym` 解决「名字 → 地址」，让算子能调用到 stub；逆向用 `dladdr` / `dl_iterate_phdr` 解决「地址 → 名字/源码」，让某个 stub 报错（或内存越界）时能把堆栈翻译回算子源码行。两者都依赖进程的动态符号表。

#### 4.2.3 源码精读

正向绑定的关键一行——用 `RTLD_DEFAULT` 句柄、按渲染出的符号名 `buf` 查地址：

[cpudebug/src/regfwk/stub_reg.cpp:L59-L60](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp#L59-L60) —— `dlsym(RTLD_DEFAULT, buf)` 在全进程范围内按名字取函数地址，再强转为统一的 `PfIntriFun` 类型写入函数表。`RTLD_DEFAULT` 意味着符号查找范围不限于某一个 `.so`，而是所有已加载的全局符号。

`buf` 这个符号名是怎么来的？由格式表 + 前缀渲染得到。格式表条目定义非常简单：

[cpudebug/include/intri_fmt.h:L23-L28](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/intri_fmt.h#L23-L28) —— 每个 `IntriFmt` 有一个 `fid` 和一个 printf 风格的格式串 `fmt`；`IntriFmtGet(fid)` 取第 `fid` 个条目（实现见生成文件 `intri_fmt.cc`）。

渲染这一步把「前缀」拼进格式串：

[cpudebug/src/regfwk/stub_reg.cpp:L51-L54](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp#L51-L54) —— `snprintf_s(buf, ..., fmt->fmt, slen, stub)` 把两个参数 `(slen, stub)` 喂给格式串。`slen` 是前缀长度，`stub` 是前缀字符串；格式串里通常含一个 `%.*s`（精度受 `slen` 控制）来把前缀拼接进来，其余部分是内建函数的固定后缀。生成脚本保证：格式串渲染出的名字，与它同时生成的 stub 函数定义名字完全一致，于是 `dlsym` 一定能命中。

逆向回溯在另一个文件里，但用的是同一族接口。崩溃时把每层栈帧的 PC 翻译成源码：

[cpudebug/src/regfwk/stub_backtrace.cpp:L102-L139](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_backtrace.cpp#L102-L139) —— `UnwindCallback` 用 `_Unwind_GetIPInfo` 取每帧 PC，用 `dladdr` 把 PC 映射到所属共享对象，再用 `popen("addr2line -e ... -f -p -a -i -C ...")` 把偏移地址翻译回「函数名 + 源码行」。

它还特意把本层基础设施从回溯里过滤掉：

[cpudebug/src/regfwk/stub_backtrace.cpp:L199-L221](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_backtrace.cpp#L199-L221) —— `IsValidBinary` 跳过 `/lib`、`/usr/lib` 等系统库，并显式跳过 `libcpudebug_stubreg.so`（也就是本讲这个库本身），避免把 stub 注册层和系统库的内部帧刷屏，让回溯只显示用户算子代码。

#### 4.2.4 代码实践

**实践目标**：把「正向绑定」和「逆向回溯」对照起来读，理解它们是同一套动态符号表的两次使用。

1. 阅读 [stub_reg.cpp:L59-L61](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp#L59-L61)，确认 `dlsym` 查到的地址会被 `dprintf` 写进 `stub_reg.log`。
2. 阅读 [stub_backtrace.cpp:L181-L196](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_backtrace.cpp#L181-L196) 的 `DlCallback`，看 `dl_iterate_phdr` 如何遍历所有已加载共享对象、记录「对象名 → 基地址」到 `BinaryBaseMap`。
3. 对照阅读单元测试 [tests/ut/testcase/regfwk/test_stub_reg.cpp:L25-L32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/regfwk/test_stub_reg.cpp#L25-L32)：它直接 `extern` 了 `g_regStubs`，调用 `StubReg(USER1, "user1")` 后断言 `g_regStubs[USER1] == "user1"`，验证了「改列名」口子的行为。

**需要观察的现象**：正向绑定（`stub_reg.log`）记录的是「字符串 → 指针」；逆向回溯（`BacktracePrint` 输出）记录的是「指针 → 字符串」。两者互为逆过程。

**预期结果**：能用自己的话解释——为什么 `dlsym` 能在 CPU 仿真里取代「手写 5149 个函数分发表」：因为符号命名是约定化的，动态链接器本身就是一张现成的、按名字索引的表。

> **待本地验证**：若你已按 [u1-l4](u1-l4-build-and-first-sample.md) 跑通 add 样例，可在样例目录下用 `nm -D` 或 `objdump -T` 查看链接的 cpudebug 库导出符号，验证符号名确实带 `AscendC` 等前缀（具体符号形态依赖闭源生成器，以本地为准）。

#### 4.2.5 小练习与答案

**练习 1**：`dlsym(RTLD_DEFAULT, buf)` 如果查不到符号，返回什么？`StubInit` 会因此失败吗？

> **参考答案**：查不到时返回 `NULL`。`StubInit` **不会**因此失败——它仍然调用 `IntriFunAdd(i, s, NULL)` 把空指针写进表。这意味着「该内建函数在该类别下没有实现」；只要算子运行时不走到那个 `(fid, type)` 单元就不会出错。若走到了空指针，才会触发崩溃，并由 `stub_backtrace` 把堆栈打印出来。

**练习 2**：为什么 `stub_backtrace.cpp` 要在 `IsValidBinary` 里显式跳过 `libcpudebug_stubreg.so`？

> **参考答案**：因为 stub 注册与调度是 cpudebug 自身的基础设施层，不属于用户算子代码。崩溃堆栈里若混入大量 stub 内部帧，会让用户难以定位「到底是算子哪一行触发了问题」。过滤掉它（以及系统库）后，回溯更聚焦在算子源码本身。

---

### 4.3 三类 stub 注册项

#### 4.3.1 概念说明

`g_regStubs` 的前三个槽位是三类**实现类别**，对应同一个内建函数的三套不同 CPU 实现：

| 下标 | 枚举 | 前缀 | 职责 | 由谁生成 |
| --- | --- | --- | --- | --- |
| 0 | `INTRI_TYPE_CPU` | `AscendC` | **功能实现**：在 CPU 上真实计算 `Add`/`DataCopy` 等的等价结果，保证算子能跑出正确输出。 | 模型库（闭源）+ `acl_stub` |
| 1 | `INTRI_TYPE_NPU` | `cceprint` | **打印跟踪**：不（只）算结果，而是把该内建函数的参数/行为打印出来，用于算子行为追踪。 | `gen_cce_stub` → `cce_stub.py` |
| 2 | `INTRI_TYPE_CCE` | `npuchk` | **运行时校验**：检查该内建函数的使用是否合法（如搬运是否越界、EnQue/DeQue 是否配对、GM 是否多核踩踏），发现即报错。 | `gen_npuchk_stub` → `write_npuchk.py` |

也就是说，**同一个 `Add`，在 CPU 域有三个名字**：`AscendC…Add…`（真算）、`cceprint…Add…`（打印）、`npuchk…Add…`（检查）。`StubInit` 用三个前缀各跑一遍 5149 个格式，把它们分别填进函数表的第 0、1、2 列。算子运行时按当前调试模式选用某一列（具体由生成代码里的调度逻辑决定，属闭源部分）。

后两列 `USER1` / `USER2` 默认为空（`nullptr`），预留给二次开发：调用 `StubReg` 填入自定义前缀，就能在不改 cpudebug 主干的前提下挂入第四、第五套实现。

#### 4.3.2 核心流程

三类 stub 的「产出」与「装配」分两阶段：

```text
构建期（CMake，fun.cmake）：
   gen_stubs     → write_stub.py   → stub 头文件
   gen_intris    → reg_funs_gen.py → intri_fun.cc（函数表骨架）+ intri_fmt.cc（5149 个格式）
   gen_cce_stub  → cce_stub.py     → cceprint 类 stub 实现
   gen_npuchk_stub → write_npuchk.py → npuchk 类 stub 实现
   上述生成文件 + stub_reg.cpp + stub_base.cpp + stub_backtrace.cpp
     → 编译为 libcpudebug_stubreg.so

运行期（StubInit）：
   for 每个类别前缀 (AscendC / cceprint / npuchk):
       for 每个内建函数格式:
           渲染符号名 → dlsym 查地址 → 填入函数表对应列
```

`npuchk` 这一列还有一个**编译期开关**控制是否启用：

```text
kern_fwk.h:
   #ifndef ASCENDC_NPUCHK_OFF
       AscendCKernelBegin(...)        // npuchk 的 kernel 开始钩子
       AscendCNpuCheckEnInterruptExit()
   #endif
```

即 npuchk 不是无脑挂载，而是可以通过定义 `ASCENDC_NPUCHK_OFF` 把整条 npuchk 钩子链关掉，仅保留功能实现列。

#### 4.3.3 源码精读

前缀数组本身就声明了这三类（再列一次以便对照）：

[cpudebug/src/regfwk/stub_reg.cpp:L25-L29](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp#L25-L29) —— 三个默认前缀分别落在下标 0/1/2，正好对应 `IntriTypeT` 的 `INTRI_TYPE_CPU` / `INTRI_TYPE_NPU` / `INTRI_TYPE_CCE`。

三类实现的生成脚本入口，全部位于闭源目录 `${CPULIB_SRC_DIR}/model/scripts/`：

[cpudebug/cmake/fun.cmake:L18-L32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/fun.cmake#L18-L32) —— `gen_stubs`（写 stub 头）、`gen_intris`（生成函数/格式两张表）、`gen_cce_stub`（生成 cceprint 实现）、`gen_npuchk_stub`（生成 npuchk 实现）。这四个 CMake 函数都走 `gen_cmd_common`，即 `python3 <脚本> <配置> <输出>` 的自定义命令，在构建时产出 `.cc` / `.h` 文件。

这些生成产物最终被汇编进一个独立的共享库 `libcpudebug_stubreg.so`：

[cpudebug/src/regfwk/CMakeLists.txt:L26-L31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/CMakeLists.txt#L26-L31) —— 该库由开源的 `stub_base.cpp` / `stub_reg.cpp` / `stub_backtrace.cpp` / `kernel_print_lock.cpp`，加上生成出来的 `intri_fun.cc` / `intri_fmt.cc`，以及 `kernel_fp16.cpp` 共同组成。换句话说：**开源代码提供注册驱动，生成代码提供被注册的数据与实现**。

`npuchk` 列在运行入口处的开关：

[cpudebug/include/kern_fwk.h:L66-L71](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L66-L71) —— `AscendCNpuCheckEnInterruptExit` 只在定义了 `ASCENDC_NPUCHK_INTER_EXIT` 时才真正调用 `AscendCEnInterruptExit()`，否则是空函数。这是 npuchk 「可关停」的细粒度开关之一。

[cpudebug/include/kern_fwk.h:L97-L100](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L97-L100) —— `StubInit` 紧随其后，整个 `AscendCKernelBegin` / `AscendCNpuCheckEnInterruptExit` 这条 npuchk 钩子链被 `#ifndef ASCENDC_NPUCHK_OFF` 包裹，可整体关闭。npuchk 工具的具体错误类型与产物（`*_npuchk.log`）详见 [u5-l1](u5-l1-npuchk-error-system.md)。

#### 4.3.4 代码实践

**实践目标**：把「三类前缀」与「三个生成脚本」一一对应起来，理解它们是同一件事的构建期与运行期两面。

1. 在 [stub_reg.cpp:L25-L29](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp#L25-L29) 圈出三个前缀。
2. 在 [fun.cmake:L18-L32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/fun.cmake#L18-L32) 找到四个 `gen_*` 函数，按本节表格填入对应关系：`gen_intris`→函数/格式表，`gen_cce_stub`→cceprint 实现，`gen_npuchk_stub`→npuchk 实现。
3. 阅读 [kern_fwk.h:L97-L100](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L97-L100)，确认 npuchk 钩子链受 `ASCENDC_NPUCHK_OFF` 控制。

**需要观察的现象**：`StubInit` 的外层循环变量 `s` 同时承担两个身份——它既是 `g_regStubs` 的下标（决定前缀），也是 `IntriFunAdd` 的 `type` 参数（决定写入第几列）。三类实现的区别，全部由「前缀字符串」这唯一变量驱动。

**预期结果**：能画出一张表，左列是 `AscendC / cceprint / npuchk`，右列依次是「功能实现 / 打印跟踪 / 运行时校验」及各自生成脚本，并解释为何同一份 `StubInit` 代码能同时装配这三类实现——因为前缀是数据，循环逻辑对三类一视同仁。

> **待本地验证**：生成脚本位于闭源 `model/scripts/`，本仓库不包含其源码；上表对应关系依据 CMake 调用名推断，若你需要确认某个具体 cceprint/npuchk 符号的行为，请在本地构建产物（生成的 `.cc` 文件或 `libcpudebug_stubreg.so` 的导出符号）中核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `StubInit` 用「前缀 + 循环」而不是为每个内建函数写一行注册代码？

> **参考答案**：因为有 5149 个内建函数重载、且分 3（~5）类实现，手写意味着上万行重复代码。前缀化后，三类实现共用同一段循环，区别只是一根字符串；新增一个内建函数时，只需生成脚本多产出一行格式与对应符号定义，`StubInit` 主干零改动。

**练习 2**：如果你想做一套「记录每个内建函数调用耗时」的实现，应该改哪一处？

> **参考答案**：不必改 `StubInit` 主干。写一组带固定前缀（如 `"perf"`）的包装函数，在每个内建函数前后记录时间戳，然后在 `StubInit` 之前调用 `StubReg(INTRI_TYPE_USER1, "perf")`（或替换 `AscendC` 列），让 `dlsym` 把这一列绑到你的包装实现上。这正是 `USER1` / `USER2` 两个空槽位的用意。

---

## 5. 综合实践

**任务**：以本讲规格里的核心任务为线索，把 `g_regStubs` × `IntriFmtGet` × `dlsym` × `IntriFunAdd` 串成一条完整的「符号解析与入表」链路，并产出一份追踪说明。

具体步骤：

1. **定位数据**。打开 [stub_reg.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp)，标出四处：`g_regStubs` 数组（L25-L29）、外层类别循环（L46-L47）、内层格式循环（L52-L53）、入表语句（L59-L60）。

2. **选一个具体类别追踪**。以 `s = 0`（前缀 `"AscendC"`）为例：`stub = "AscendC"`，`slen = 7`。

3. **选一个具体内建函数追踪**。任取一个 `i`（例如 `i = 0`）：
   - `IntriFmtGet(0)` 返回第一个 `IntriFmt{fid=0, fmt="<格式串>"}`；
   - `snprintf_s(buf, ..., fmt, 7, "AscendC")` 把前缀拼进格式串，得到一个完整符号名（设为 `<SYM>`，具体形态以生成器为准，属闭源细节）；
   - `dlsym(RTLD_DEFAULT, "<SYM>")` 在进程已加载的所有库里查 `<SYM>` 的地址 `fun`；
   - `IntriFunAdd(0, INTRI_TYPE_CPU, fun)` 把 `fun` 写入函数表的 `(fid=0, type=0)` 单元。

4. **回答三个问题**（写入你的学习笔记）：
   - 这个内建函数在「打印跟踪」和「运行时校验」两列里分别会被填进哪个单元？地址从哪两个前缀的符号查得？
   - 若 `dlsym` 返回 `NULL`，函数表对应单元是什么？算子在什么情况下才会因此出错？
   - 整张表填满后，算子源码里 `Add(...)` 是如何被引导到表里某个函数指针的？（提示：生成代码里的调度逻辑按当前调试模式选列，属闭源部分，可标注「待确认」。）

5. **交叉验证**。按 [u1-l4](u1-l4-build-and-first-sample.md) 跑一次 add 样例，打开产物 `stub_reg.log`，找到与你追踪的 `i` 对应的那几行（`AscendC: [...] -> ...`、`cceprint: [...] -> ...`、`npuchk: [...] -> ...`），核对「同一 `fid`、三个前缀、三个地址」的结构是否符合预期。

**预期产出**：一段 200 字左右的追踪说明 + 一张「`(fid, type)` 单元 ← 前缀 ← 生成脚本」的对照表。若无法本地运行样例，至少完成步骤 1–4 的纯源码追踪，并在步骤 4 第三问标注「待本地验证」。

## 6. 本讲小结

- cpudebug 用一张 **`(fid, type)` 二维函数表**（规模 \(5149 \times 5\)）替代手写的内建函数分发表；`StubInit` 负责在 fork 之前一次性把它填满。
- 注册主循环遍历「5 个类别 × 5149 个格式」，核心三步是：`IntriFmtGet` 取格式 → `snprintf_s` 渲染符号名 → `dlsym(RTLD_DEFAULT, ...)` 查地址 → `IntriFunAdd` 入表，并把每个绑定写进 `stub_reg.log`。
- `dlsym` 是「正向绑定」（名字→地址），`stub_backtrace.cpp` 里的 `dladdr` / `dl_iterate_phdr` + `addr2line` 是「逆向回溯」（地址→源码），两者是同一套动态符号表机制的两次使用。
- 三类实现由三个前缀驱动：`AscendC`=功能实现、`cceprint`=打印跟踪、`npuchk`=运行时校验；它们分别由闭源脚本 `reg_funs_gen.py` / `cce_stub.py` / `write_npuchk.py` 在构建期生成。
- `StubReg` + `USER1` / `USER2` 两个空列提供了不改主干即可挂入自定义实现的扩展点；`ASCENDC_NPUCHK_OFF` 提供了关停 npuchk 钩子链的编译期开关。
- 开源/闭源边界清晰：`stub_reg.cpp` 等是开源的「注册驱动」，`intri_fmt.cc` / `intri_fun.cc` 及各 stub 实现是闭源「生成产物」，两者一起编译进 `libcpudebug_stubreg.so`。

## 7. 下一步学习建议

- 接下来推荐学 **[u3-l4 浮点数据类型的 CPU 仿真](u3-l4-fp-type-simulation.md)**：本讲的 `AscendC` 功能实现列里，`Add` 之所以能在 CPU 上算出 fp16/bf16/fp8 的正确结果，靠的就是 `kernel_fp16.h` / `kernel_bf16.h` 这些低精度类型仿真头；可以顺着 `stub_base.cpp` 引入的 `kernel_fp16.h` 往下读。
- 若你对 `npuchk` 这一列「运行时校验」到底检查什么、产物 `*_npuchk.log` 长什么样更感兴趣，可以跳到 **[u5-l1 npu check 错误体系与检查机制](u5-l1-npuchk-error-system.md)** 和 **[u5-l2 日志解析与源码行定位](u5-l2-log-parsing-addr2line.md)**——后者正是用 `addr2line` 解析本讲 `stub_backtrace.cpp` 产出的堆栈。
- 想理解 `libcpudebug_stubreg.so` 如何与其他库一起被打包、安装、建立软链，可阅读 **[u9-l1 CMake 构建系统与多架构产物](u9-l1-cmake-multi-arch.md)** 与 **[u9-l2 打包安装与 run 包生成](u9-l2-package-install.md)**。
- 想动手扩展一个自定义 stub 列（填入 `USER1`），可参考 **[u10-l1 扩展 API 校验器与二次开发](u10-l1-extend-api-check.md)** 了解开源/闭源边界对二次开发的约束。
