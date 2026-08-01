# 核心命令行工具一览

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `opt`、`llc`、`llvm-as`、`llvm-dis`、`lli` 这五个核心工具**各自**的职责，并能在源码里指出它们的入口与「真正干活」的位置。
- 把一条「文本 IR → 优化 → 代码生成 → 执行」的命令链**拼出来**，并能解释每一步输入和输出分别是什么文件、什么格式。
- 区分两个最容易混淆的「驱动（driver）」：`opt` 是**优化驱动**（在 IR 层面跑 pass），`llc` 是**代码生成驱动**（把 IR 翻成目标机器码）。
- 理解这些工具本质上都是 [u1-l2](u1-l2-directory-structure.md) 讲过的「薄壳工具」——`main` 只负责解析命令行、把活转发给 `lib/` 里的库。

本讲承接 [u1-l1](u1-l1-project-overview.md)（项目定位）与 [u1-l2](u1-l2-directory-structure.md)（目录结构）。前置讲义已经告诉你 `tools/` 下放的是「薄壳」、`lib/` 下放的是「实现」，本讲要回答的是：**这些薄壳工具具体是怎么协作的、它们在「源码 → IR → 机器码」这条流水线上各自站在哪一站**。

## 2. 前置知识

先用大白话讲清三个贯穿全讲的概念。

**IR 有三种存在形态。** 同一份「LLVM 中间表示（IR）」，可以以三种方式存在：

1. **内存中的 `Module` 对象**：编译时真正被处理的 C++ 对象，是 `include/llvm/IR/Module.h` 里那个类（这个对象是后续 [u3-l1](u3-l1-ir-hierarchy.md) 的主角，这里只需知道它是「IR 在内存里的样子」）。
2. **`.ll` 文本汇编**：人类可读的文本形式，用 `;` 注释、`%` 给寄存器命名，长得很像汇编。你可以用任意编辑器打开它。
3. **`.bc` 位码（bitcode）**：紧凑的二进制形式，体积小、解析快，适合落盘和分发。

这三种形态表达的是**同一份 IR**，彼此之间可以无损互转。本讲的工具，本质就是这三种形态之间的「转换器」和「处理器」。

**「驱动（driver）」是什么意思？** 在 LLVM 语境里，"driver" 指一个**编排者**：它自己不做具体的优化或翻译，而是把一组「pass（变换步骤）」按顺序拼成一条流水线（pipeline），然后让 `lib/` 里的代码去执行。`opt` 和 `llc` 都是 driver，它们的价值在于「编排」，真正的算法都在库里。这正好印证 [u1-l2](u1-l2-directory-structure.md) 的结论：工具是薄壳，逻辑下沉到 `lib/`。

**pass 是什么？** 一个 pass 就是流水线上的一道工序，例如「指令合并」「把循环里的不变量提到循环外」。一个 driver 跑一次，通常会把几十上百个 pass 依次过一遍。本讲你只需把 pass 理解为「流水线上的一道工序」；如何自己写 pass 是 [u4-l4](u4-l4-write-your-own-pass.md) 的主题。

## 3. 本讲源码地图

本讲围绕五个工具的入口文件展开，外加 `opt` 真正转发过去的那份驱动实现：

| 文件 | 作用 |
| --- | --- |
| [llvm/tools/llvm-as/llvm-as.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llvm-as/llvm-as.cpp) | **汇编器**：读 `.ll` 文本，解析成内存 `Module`，再写成 `.bc` 位码。 |
| [llvm/tools/llvm-dis/llvm-dis.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llvm-dis/llvm-dis.cpp) | **反汇编器**：读 `.bc` 位码，还原成 `.ll` 文本。和 `llvm-as` 互为逆操作。 |
| [llvm/tools/lli/lli.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/lli/lli.cpp) | **解释器 / 即时编译器**：直接「跑」一份 IR，调用其中的 `main`，返回结果。 |
| [llvm/tools/opt/opt.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/opt.cpp) | **优化驱动的薄壳**：`main` 只有一行，把一切转发给 `optMain`。 |
| [llvm/tools/opt/NewPMDriver.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/NewPMDriver.cpp) | `opt` 真正的驱动实现所在：解析 `-passes=...` 文本流水线、构造 `PassBuilder` 并运行。 |
| [llvm/tools/llc/llc.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llc/llc.cpp) | **代码生成驱动**：读 IR，经后端流水线翻译成目标汇编（`.s`）或目标文件（`.o`）。 |

> 说明：本讲的「最小模块」是两个——4.1 讲 `llvm-as / llvm-dis / lli`（格式转换 + 直接执行），4.2 讲 `opt 与 llc`（优化驱动 + 代码生成驱动）。`clang` 会在概述里作为「产出 IR 的前门」被提及，但它的源码深挖留到 [u5](#7-下一步学习建议)。

## 4. 核心概念与源码讲解

先把全局图记住：这五个工具都是围绕「IR 三种形态」的转换与处理而生的。下图是本讲的「地图」，后续两节就是把它拆开讲。

```
                     (clang -S -emit-llvm 产生 IR，详见 u5)
   C/C++ 源码 ──────────────────────────────────────────────┐
                                                              ▼
                          ┌─────────── .ll (文本) ────────────┐
                          │                                    │
                  llvm-as │ ▲                          ▼ │ llvm-dis
   (文本→位码) ───────────┘ │                          │ └────────── (位码→文本)
                          │ │                          │ │
                          ▼ │                          ▼ │
                          └─────────── .bc (位码) ───────┘
                          │      ▲                ▲
                  opt 优化 │      │                │ llc 生成机器码
            (IR→更优的 IR) ▼      │                │ ▼
                          ┌── 内存 Module ──┐       │ .s 汇编 / .o 目标文件
                          │                 │       │
                          └── lli 直接执行 ──┘       (再经汇编器/链接器→可执行文件)
```

下面两节分别打开两个最小模块。

### 4.1 llvm-as / llvm-dis / lli：IR 的格式转换与直接执行

#### 4.1.1 概念说明

这三个工具相对简单，是理解整个工具链的「入门三件套」。

- **`llvm-as`（assembler，汇编器）**：把人类可读的 `.ll` 文本「汇编」成机器友好的 `.bc` 位码。注意这里的 "assembler" 不是指汇编语言（x86 汇编那种），而是「把文本 IR 装配成二进制 IR」的意思。它做两件事：**解析（parse）**文本 → 内存 `Module`，再**写出（write）**成位码。
- **`llvm-dis`（disassembler，反汇编器）**：`llvm-as` 的逆操作，把 `.bc` 位码「反汇编」回 `.ll` 文本。它做两件事：**读入（read）**位码 → 内存 `Module`，再**打印（print）**成文本。
- **`lli`（LLVM interpreter / dynamic compiler）**：不再落盘成 `.s`/`.o`，而是**直接在内存里把 IR 跑起来**。它默认会去找模块里的 `main` 函数并执行，把返回值作为进程退出码。它内部走的是即时编译（JIT）——默认使用 ORC JIT（详见 [u8-l1](u8-l1-executionengine-orc-jit.md)）。

三者有一个共同的关键点：**它们都把 IR 读进同一个内存对象 `Module`**。区别只在于「读进来之后干什么」——`llvm-as` 写位码、`llvm-dis` 写文本、`lli` 编译并执行。这个「以 `Module` 为中介」的设计，正是三种 IR 形态能无损互转的根本原因。

#### 4.1.2 核心流程

**`llvm-as` 的流程**（`.ll` → `.bc`）：

```
读 .ll 文件
   │
   ▼
parseAssemblyFileWithIndex()   ← 解析文本，得到内存 Module（+ 可选的摘要索引）
   │
   ▼
verifyModule()                 ← 校验 IR 是否合法（不合法直接报错退出）
   │
   ▼
WriteBitcodeToFile()           ← 把 Module 序列化成 .bc 位码写盘
```

**`llvm-dis` 的流程**（`.bc` → `.ll`）：

```
读 .bc 文件
   │
   ▼
getBitcodeFileContents()       ← 读出位码容器内容
   │
   ▼
getLazyModule() + materializeAll()  ← 构造 Module 并物化全部内容到内存
   │
   ▼
M->print()                     ← 把内存 Module 打印成 .ll 文本写盘
```

**`lli` 的流程**（`.bc`/`.ll` → 执行）：

```
初始化本机目标后端（InitializeNativeTarget）
   │
   ▼
parseIRFile()                  ← 读 IR 到内存 Module
   │
   ▼
按 --jit-kind 选择执行引擎     ← 默认走 runOrcJIT()（ORC v2 分层 JIT）
   │
   ▼
在 JIT 里编译并执行 main()      ← 返回值作为进程退出码
```

#### 4.1.3 源码精读

**（1）`llvm-as`：从解析到位码。** 文件顶部注释把用途说得非常直白——「读 `.ll`、写 `.bc`」：

[llvm/tools/llvm-as/llvm-as.cpp:L1-L15](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llvm-as/llvm-as.cpp#L1-L15) 这段注释说明：从 `.ll` 读入、写到同名 `.bc`。

它的 `main` 很短，正好印证「薄壳」的说法。核心三步是「解析 → 校验 → 写出」：

[llvm/tools/llvm-as/llvm-as.cpp:L123-L130](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llvm-as/llvm-as.cpp#L123-L130) 调用 `parseAssemblyFileWithIndex(...)` 把文本 IR 解析成内存 `Module`。这一步的真正实现在 `lib/AsmParser/`（见 [u3-l5](u3-l5-asm-bitcode.md)），`llvm-as` 只是调用者。

[llvm/tools/llvm-as/llvm-as.cpp:L139-L149](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llvm-as/llvm-as.cpp#L139-L149) 解析完成后立即用 `verifyModule(...)` 校验 IR 合法性。校验失败会报 "assembly parsed, but does not verify as correct!" 并退出——这保证 `llvm-as` 产出的位码一定是合法 IR。

[llvm/tools/llvm-as/llvm-as.cpp:L98](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llvm-as/llvm-as.cpp#L98) 校验通过后调用 `WriteBitcodeToFile(*M, ...)`，把内存 `Module` 序列化为 `.bc` 位码。这一步的实现在 `lib/Bitcode/Writer/`。

> 顺带一提，`llvm-as` 的输出文件名是「智能推断」的：输入 `x.ll`，自动输出 `x.bc`（见同文件 `WriteOutputFile` 里把 `.ll` 后缀替换成 `.bc` 的逻辑）。

**（2）`llvm-dis`：从位码到文本，恰好相反。** 顶部注释同样直白：

[llvm/tools/llvm-dis/llvm-dis.cpp:L1-L32](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llvm-dis/llvm-dis.cpp#L1-L32) 说明它是 `.bc → .ll` 的反汇编器，并列出了 `-o`、`--show-annotations` 等选项。

`main` 里同样三步「读 → 物化 → 打印」，注意它一次可以处理多个输入文件：

[llvm/tools/llvm-dis/llvm-dis.cpp:L204](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llvm-dis/llvm-dis.cpp#L204) 用 `getBitcodeFileContents(...)` 把位码文件内容读出来。

[llvm/tools/llvm-dis/llvm-dis.cpp:L216-L223](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llvm-dis/llvm-dis.cpp#L216-L223) 用 `getLazyModule(...)` 拿到一个「惰性」的 `Module`，再 `materializeAll()` 把内容真正加载进内存。「惰性加载」是位码 reader 的一个特性——它允许只在实际需要时才把某段 IR 读进来，这在 LTO 等大模块场景里能省时间（详见 [u8-l2](u8-l2-lto.md.md)）。

[llvm/tools/llvm-dis/llvm-dis.cpp:L266](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llvm-dis/llvm-dis.cpp#L266) 一句 `M->print(Out->os(), ...)` 就是「反汇编」的全部——把内存 `Module` 打印成文本。`Module::print` 的底层是 `AsmWriter`（见 [u3-l5](u3-l5-asm-bitcode.md)）。**这说明「写文本 IR」只是 `Module` 的一个普通方法，任何拿到 `Module` 的工具都能调用它。**

**（3）`lli`：直接把 IR 跑起来。** 顶部注释点明它的定位是「Execution Engines 的简单包装」：

[llvm/tools/lli/lli.cpp:L1-L13](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/lli/lli.cpp#L1-L13) 说明 `lli` 通过 JIT 直接执行 IR，或在无 JIT 的平台上退化为解释器。

`lli` 最值得关注的是它如何「选择执行引擎」。先看它定义的几种 JIT 模式：

[llvm/tools/lli/lli.cpp:L109-L115](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/lli/lli.cpp#L109-L115) `--jit-kind` 选项，提供 `mcjit` / `orc` / `orc-lazy` 三种模式，**默认是 `orc`**。

`main` 里据此做派发——这一段是理解 `lli` 行为的钥匙：

[llvm/tools/lli/lli.cpp:L426-L428](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/lli/lli.cpp#L426-L428) 初始化「本机原生（native）」目标后端及其汇编打印/解析器——JIT 必须先有可用后端才能把 IR 编译成当前机器能跑的机器码。

[llvm/tools/lli/lli.cpp:L445-L448](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/lli/lli.cpp#L445-L448) 派发逻辑：若显式选了 MCJIT 或 `--force-interpreter`，走旧路径；**否则（默认）直接 `return runOrcJIT(...)`**，进入 ORC v2 JIT。也就是说，现代 `lli` 默认走的是 ORC。

`lli` 默认要执行的入口函数是 `main`，可用 `--entry-function` 改写。`main` 的返回值会被作为进程退出码——这正是下一个实践里「用 `echo $?` 看结果」的依据。

#### 4.1.4 代码实践

**实践目标**：亲手完成一次 `.ll ⇄ .bc` 的来回转换，并用 `lli` 直接执行一份 IR，体会「内存 `Module` 是三种形态的中介」。

**操作步骤**（需要你已按 [u1-l3](u1-l3-build-system.md) 构建出 `llvm-as`、`llvm-dis`、`lli`，或系统已安装 LLVM 工具链）：

1. 用编辑器新建 `demo.ll`，内容如下（一个返回 `7` 的 `main`，先用两步加法把「常量折叠」的机会留给后面的 `opt`）：

   ```llvm
   ; demo.ll —— 返回整数 7 的 main 函数
   define i32 @main() {
   entry:
     %x = add i32 1, 2     ; 1 + 2
     %y = add i32 %x, 4    ; 3 + 4 = 7
     ret i32 %y
   }
   ```

   > 读不懂这串 IR 没关系，只需知道：它定义了一个返回 32 位整数（`i32`）的函数 `@main`，`%x`、`%y` 是 SSA 寄存器，`ret` 是返回。`.ll` 语法是 [u2-l2](u2-l2-read-write-ir.md) 的主题。

2. 汇编成位码，并观察产物：

   ```bash
   llvm-as demo.ll -o demo.bc        # 默认：demo.ll → demo.bc
   file demo.bc                      # 期望：LLVM IR bitcode
   xxd demo.bc | head -n 1           # 期望：开头是 4 字节魔术字
   ```

3. 反汇编回来，验证「无损往返」：

   ```bash
   llvm-dis demo.bc -o demo.roundtrip.ll
   diff demo.ll demo.roundtrip.ll    # 期望：没有差异（或仅有空白差异）
   ```

4. 直接用 `lli` 跑这份位码：

   ```bash
   lli demo.bc
   echo $?                           # 期望：7（main 的返回值）
   ```

**需要观察的现象**：`demo.bc` 是二进制（`file` 报告为 bitcode）；`demo.roundtrip.ll` 与原文件内容一致；`lli demo.bc` 的退出码为 `7`。

**预期结果**：第 2 步能看到位码的魔术字；第 3 步 `diff` 无输出；第 4 步 `echo $?` 打印 `7`。

> 若你当前环境没有这些工具的构建产物，则以上命令为「待本地验证」。文件头魔术字的确切字节、`diff` 是否完全无差异，可能与 LLVM 版本有关，以本地实测为准。

#### 4.1.5 小练习与答案

**练习 1**：`llvm-as` 在校验 IR 失败时会怎样？翻回源码确认你的判断。

参考答案：会报 "assembly parsed, but does not verify as correct!" 并以非零状态退出（见 [llvm-as.cpp:L139-L149](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llvm-as/llvm-as.cpp#L139-L149)）。也就是说，`llvm-as` 绝不会产出非法位码。

**练习 2**：不指定 `-o` 时，`llvm-as` 输入 `foo.ll`、`llvm-dis` 输入 `bar.bc`，各自默认输出什么文件名？

参考答案：`llvm-as` 输出 `foo.bc`（把 `.ll` 换成 `.bc`），`llvm-dis` 输出 `bar.ll`（把 `.bc` 换成 `.ll`）。两者都支持用 `-o` 覆盖。

**练习 3**：为什么说 `llvm-as` 和 `llvm-dis` 是「无损互逆」的？从「内存 `Module` 是中介」的角度解释。

参考答案：两者都把 IR 读进同一个内存 `Module` 再写出去，而 `Module` 是三种形态的共同中介。`.ll` 和 `.bc` 只是 `Module` 的两种序列化方式，互转过程中 `Module` 本身不变，故可无损往返（除非人为加 `--disable-verify` 或用某些保留选项，正常路径下是一致的）。

### 4.2 opt 与 llc：优化驱动与代码生成驱动

#### 4.2.1 概念说明

这两个是本讲的「重头戏」，也是最容易被初学者搞混的两个工具。它们都是上一节说的「driver（编排者）」，但编排的东西完全不同。

- **`opt`（optimizer，优化器）**：在 **IR 层面**工作。读入一份 IR（`.ll` 或 `.bc`），按你指定的 pass 流水线跑一遍优化，再输出一份 IR（默认位码，`-S` 输出文本）。**它的输入和输出都是 IR**——只不过输出通常是「更优化的 IR」。它还可以只跑「分析 pass」来打印信息而不改 IR。官方文档把它称作「模块化的 LLVM 优化器与分析器」。
- **`llc`（LLVM compiler，代码生成器）**：在 **后端层面**工作。读入一份 IR，经过指令选择、寄存器分配、指令调度等一系列后端 pass，输出**目标机器码**——默认是目标汇编 `.s`（`-filetype=obj` 可直接出目标文件 `.o`）。**它的输入是 IR，输出是机器码**，跨越了「IR → 机器」这一层。

一句话区分：

| 维度 | `opt` | `llc` |
| --- | --- | --- |
| 工作层面 | IR 层 | 后端层（IR → 机器码） |
| 输入 | IR（`.ll`/`.bc`） | IR（`.ll`/`.bc`） |
| 输出 | 优化后的 IR（`.ll`/`.bc`） | 目标汇编 `.s` 或目标文件 `.o` |
| 典型用法 | `opt -passes=instcombine,gvn ...` | `llc -O2 demo.ll -o demo.s` |

两者还有一个共同特征：**它们都是 [u1-l2](u1-l2-directory-structure.md) 意义上的「薄壳」**。`opt.cpp` 是本讲最极端的例子——`main` 只有一行；`llc.cpp` 稍厚，但真正的编译逻辑也都在 `lib/CodeGen/` 等库里。后面源码精读会直接看到这一点。

#### 4.2.2 核心流程

**`opt` 的流程**（IR → 优化 → IR）：

```
读 .ll 或 .bc → 内存 Module
   │
   ▼
解析 -passes="instcombine,gvn,..." 文本流水线
   │
   ▼
构造 PassBuilder（注册所有可用 pass 与扩展点）
   │
   ▼
把文本流水线翻译成 PassManager 内的实际 pass 序列
   │
   ▼
PassManager.run(Module)        ← 依次跑每个 pass，改写 IR
   │
   ▼
输出优化后的 IR（-S → .ll，否则 → .bc）
```

**`llc` 的流程**（IR → 机器码）：

```
初始化全部目标后端（InitializeAllTargets / AsmPrinters / ...）
   │
   ▼
读 IR → 内存 Module；按目标三元组创建 TargetMachine
   │
   ▼
TargetMachine::addPassesToEmitFile(...)   ← 把一整套后端 pass 装进 PassManager
   │
   ▼
PassManager.run(Module)                    ← 跑后端流水线：指令选择→寄存器分配→...
   │
   ▼
经 MC 层发射 .s 汇编（默认）或 .o 目标文件
```

注意两者的对称与不对称：都用 `PassManager` 编排 pass，但 `opt` 的 pass 改写的是 IR，`llc` 的 pass 把 IR 一路下沉到机器码。后端流水线的细节（SelectionDAG、GlobalISel、MC 层）是 [u6](#7-下一步学习建议) 单元的主题，本讲只看 `llc` 这个「驱动」如何把它们组织起来。

#### 4.2.3 源码精读

**（1）`opt` 是极致的「薄壳」。** 整个 `opt.cpp` 没有任何编译逻辑，`main` 直接转发：

[llvm/tools/opt/opt.cpp:L23-L27](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/opt.cpp#L23-L27) `main` 仅一句 `return optMain(argc, argv, {})`。真正的驱动逻辑在 `NewPMDriver.cpp` 的 `optMain`（之所以这样拆，是为了让 `clang` 等其它程序也能复用 `optMain`，从而把同一套优化流水线嵌进去）。

「跑流水线」的核心是 `runPassPipeline`。先看它的签名，就能看出 `opt` 都管了什么：

[llvm/tools/opt/NewPMDriver.cpp:L355-L364](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/NewPMDriver.cpp#L355-L364) 参数里有 `Module &M`（要处理的 IR）、`PassPipeline`（`-passes=...` 那串文本）、若干 `PassPlugin`（动态插件，见 [u9-l2](u9-l2-pass-plugins.md)）、以及输出/校验相关开关。这就是 `opt` 全部职责的「目录」。

函数内部构造了各类分析管理器和那个关键的 `PassBuilder`：

[llvm/tools/opt/NewPMDriver.cpp:L461](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/NewPMDriver.cpp#L461) `PassBuilder PB(TM, PTO, P, &PIC)`——`PassBuilder` 是「pass 的总注册表」，它知道如何把 `-passes=instcombine,gvn` 这样的文本解析成真实的 pass 对象序列。新 Pass 管理器的全貌是 [u4-l1](u4-l1-new-pass-manager.md) 的主题。

> 结论：`opt` 的全部「智能」都来自 `PassBuilder` 和 `lib/Transforms/` 里的 pass 实现；工具本身只是「读 IR → 调 PassBuilder 跑流水线 → 写 IR」的胶水。

**（2）`llc`：先初始化所有后端，再编译。** 顶部注释点明它是「代码生成驱动」：

[llvm/tools/llc/llc.cpp:L1-L13](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llc.cpp#L1-L13) 说明 `llc` 是个命令行驱动，给定 IR，产出汇编或可重定位文件。

`main` 一上来就「初始化全部目标」，这一步和 `lli` 形成鲜明对比——`lli` 只要「本机原生」一个目标，`llc` 要「全部」，因为它可能被要求为任意架构生成代码：

[llvm/tools/llc/llc.cpp:L378-L381](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llc/llc.cpp#L378-L381) 依次调用 `InitializeAllTargets()`、`InitializeAllTargetMCs()`、`InitializeAllAsmPrinters()`、`InitializeAllAsmParsers()`，把所有已编译进来的后端都注册好。你在 [u1-l3](u1-l3-build-system.md) 里用 `LLVM_TARGETS_TO_BUILD` 选定的那些架构，就是在这里「能被用到」的。

`llc` 的优化级别用一个 `-O` 选项表达，默认 `-O2`：

[llvm/tools/llc/llc.cpp:L126-L130](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llc/llc.cpp#L126-L130) 定义 `-O` 选项（`-O0..-O3`，默认 `2`）。这个级别会传给 `TargetMachine`，影响后端流水线启用哪些 pass。

`main` 的主体把活交给 `compileModule`：

[llvm/tools/llc/llc.cpp:L459-L461](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llc/llc.cpp#L459-L461) 在循环里调用 `compileModule(...)`（`for` 循环是为了 `-time-compilations` 重复编译计时）。`compileModule` 是 `llc` 真正干活的地方。

`compileModule` 先读 IR、再创建 `TargetMachine`：

[llvm/tools/llc/llc.cpp:L670-L673](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llc/llc.cpp#L670-L673) 用 `parseIRFile(...)` 把 `.ll`/`.bc` 读成内存 `Module`——和 `lli` 用的是同一个函数。读进来之后，再依据模块的目标三元组（triple）去 `TargetRegistry` 里查出对应的 `Target` 并创建 `TargetMachine`。

最后，把一整套后端 pass 装进 `PassManager` 并运行：

[llvm/tools/llc/llc.cpp:L839-L841](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llc/llc.cpp#L839-L841) `Target->addPassesToEmitFile(PM, ...)` 这一句是 `llc` 的核心：它让「目标后端」把自己需要的那一长串后端 pass（指令选择、寄存器分配、指令调度、MC 发射……）一次性塞进 `PM`。随后 `PM.run(*M)`（见 [llc.cpp:L880](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llc/llc.cpp#L880)）就真正把 IR 一路翻译成了 `.s` 或 `.o`。

> 对比 `opt` 与 `llc`：都用 `PassManager.run(Module)`，但 `opt` 的 pass 来自 `PassBuilder` 对文本流水线的解析（在 IR 层改写），`llc` 的 pass 来自 `TargetMachine::addPassesToEmitFile`（把 IR 下沉到机器码）。这正是「优化驱动」与「代码生成驱动」在源码层面的根本区别。

#### 4.2.4 代码实践

**实践目标**：用 `opt` 看到「优化真的发生了」，再用 `llc` 看到「IR 变成了机器码」，从而直观区分两个驱动。

**操作步骤**（沿用 4.1.4 里那个 `demo.ll`）：

1. 用 `opt` 跑一次指令合并（`instcombine`），把 `1+2`、`+4` 折叠：

   ```bash
   opt -passes=instcombine -S demo.ll -o demo.opt.ll
   cat demo.opt.ll
   ```

2. 用 `llc` 生成目标汇编：

   ```bash
   llc demo.ll -o demo.s         # 默认输出 .s（目标汇编）
   head -n 40 demo.s             # 找到 main: 标签
   ```

3. （可选）让 `llc` 直接出目标文件，并反汇编观察：

   ```bash
   llc -filetype=obj demo.ll -o demo.o
   llvm-objdump -d demo.o | head    # 看到 main 的机器指令
   ```

**需要观察的现象**：
- 第 1 步：`demo.opt.ll` 里的 `main` 应该被「压扁」——两个 `add` 和中间寄存器消失，`ret` 直接返回常量 `7`（形如 `ret i32 7`）。这就是 `instcombine` 的常量折叠效果。
- 第 2 步：`demo.s` 是一段 x86（或你本机架构）汇编，里面有 `main:` 函数，结尾把返回值放进 `%eax`/`%rax` 后 `ret`。由于后端也会做常量折叠，你大概率会看到直接把 `7` 装进返回寄存器的指令（形如 `movl $7, %eax`）。
- 第 3 步：`llvm-objdump -d` 输出的是真正的机器指令字节 + 反汇编。

**预期结果**：`opt` 步骤得到「`ret i32 7`」；`llc` 步骤得到目标汇编文件。两者输入是同一份 IR，但一个产出「更优的 IR」、一个产出「机器码」——这就是优化驱动与代码生成驱动的差别。

> 不同架构、不同 LLVM 版本下，`.s` 的确切指令（`movl`/`movw`、寄存器名等）会有差异，以本地实测为准；若本地无构建产物，则上述为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`opt -passes=instcombine` 不带 `-S` 时，输出是什么格式？为什么？

参考答案：输出是 `.bc` 位码（二进制），因为 `opt` 默认输出位码；加 `-S` 才输出人类可读的 `.ll` 文本。这与 `llvm-as` 默认写位码是一致的约定——「文本要显式 `-S` 请求」。

**练习 2**：`opt` 和 `llc` 都会调用 `PassManager.run(Module)`，为什么产物截然不同？

参考答案：因为塞进 `PassManager` 的 pass 不同。`opt` 的 pass 来自 `PassBuilder` 对 `-passes=...` 文本流水线的解析，作用在 IR 层（改写 IR、输出仍是 IR）；`llc` 的 pass 来自 `TargetMachine::addPassesToEmitFile`，是一整套后端 pass，作用是把 IR 一路下沉成机器码（输出 `.s`/`.o`）。`PassManager` 只是「按序跑 pass」的通用执行器，跑什么 pass 决定了产物。

**练习 3**：为什么 `llc` 在 `main` 里要 `InitializeAllTargets()`，而 `lli` 只 `InitializeNativeTarget()`？

参考答案：因为定位不同。`lli` 只在本机直接执行代码，只需要「本机原生」一个后端就够；`llc` 是代码生成驱动，可能被要求为任意目标架构（交叉编译）生成代码，所以必须把所有已编译进来的后端都注册好。这也对应 [u1-l3](u1-l3-build-system.md) 里 `LLVM_TARGETS_TO_BUILD` 决定「哪些架构可用」——选少了，`llc` 也就编不出对应目标的代码。

## 5. 综合实践

把本讲五个工具串成一条**端到端流水线**，亲手走一遍「文本 IR → 位码 → 优化 → 机器码 → 执行」。

**任务**：基于 4.1.4 的 `demo.ll`，按下面的链条依次处理，并在每一步记录「输入文件、输出文件、文件类型（文本/二进制）、关键变化」。

```bash
# 0. 起点文本 IR
cat demo.ll                       # 文本

# 1. 文本 → 位码
llvm-as demo.ll -o demo.bc
file demo.bc                      # 二进制 bitcode

# 2. 优化（IR → 更优的 IR，输出文本以便观察）
opt -passes=instcombine -S demo.ll -o demo.opt.ll
grep 'ret i32' demo.opt.ll        # 期望看到 ret i32 7

# 3. 优化后的 IR → 目标汇编
llc demo.opt.ll -o demo.s
grep -A3 '^main:' demo.s          # 期望看到 main 的汇编

# 4. 目标汇编 → 目标文件 → 可执行（用系统汇编器/链接器，或直接用 clang 当驱动）
clang demo.s -o demo              # 也可：llc -filetype=obj 后用 ld/lld 链接
./demo ; echo $?                  # 期望：7

# 5. 对照：跳过落盘，直接执行
lli demo.bc ; echo $?             # 期望：7（与第 4 步一致）
```

**要回答的问题**（写进你的学习笔记）：

1. 第 1 步产物和第 0 步产物，哪个是人类可读的？为什么 IR 需要两种形态？
2. 第 2 步发生了什么「实质变化」？是哪一类工具带来的？
3. 第 4 步和第 5 步得到同样的结果 `7`，但走了完全不同的路径——分别经过了哪些 IR 形态、绕过了哪些形态？
4. 在这张图里，`opt` 和 `llc` 各自只占了「相邻两站」，这说明了「driver 编排 pass」的什么特点？

**预期结果**：一条从 `demo.ll` 出发、最终在屏幕上打印 `7` 的完整链路；你能用一句话说清每个工具在链路上「把什么转成什么」。

> 提示：第 4 步若本机没有 `clang`，可改用「`llc -filetype=obj demo.opt.ll -o demo.o` + 系统链接器」，或干脆只做到第 3 步（得到 `.s`）并在笔记里说明后续步骤。命令的确切输出以本地实测为准。

## 6. 本讲小结

- LLVM IR 有三种形态——**内存 `Module`**、**`.ll` 文本**、**`.bc` 位码**，三者以 `Module` 为中介可无损互转；本讲的工具本质都是这三种形态之间的「转换器」和「处理器」。
- **`llvm-as`**（`.ll`→`.bc`）与 **`llvm-dis`**（`.bc`→`.ll`）互为逆操作，流程都是「读进 `Module` → 写出去」，区别只在写位码还是写文本。
- **`lli`** 不落盘成机器码，而是通过 JIT（默认 ORC v2）**直接执行** IR，默认入口是 `main`，返回值作为退出码。
- **`opt`** 是**优化驱动**：读 IR、用 `PassBuilder` 解析 `-passes=...` 文本流水线、跑 pass、输出更优的 IR；它的全部「智能」在 `PassBuilder` 和 `lib/Transforms/`。
- **`llc`** 是**代码生成驱动**：读 IR、用 `TargetMachine::addPassesToEmitFile` 装配一整套后端 pass、跑出 `.s`/`.o`；它和 `opt` 都用 `PassManager.run`，但塞进去的 pass 决定了产物。
- 这些工具都是 [u1-l2](u1-l2-directory-structure.md) 意义上的**薄壳**——`opt.cpp` 的 `main` 只有一行，真正的逻辑全在 `lib/` 与 `tools/opt/NewPMDriver.cpp`、`llc.cpp::compileModule` 之外的库里。

## 7. 下一步学习建议

本讲让你建立了「源码 → IR → 机器码」的工具链直觉，但有几个问题被刻意搁置了：

- **IR 到底长什么样、怎么读、怎么写？** 下一讲 [u2-l2 阅读与编写 LLVM IR](u2-l2-read-write-ir.md) 会系统讲 `.ll` 的语法（基本块、SSA、phi、类型标注）。在那之前，[u2-l1 三段式编译器设计与 IR 的角色](u2-l1-three-phase-design.md) 会从架构层面解释「为什么需要 IR 这个中间层」——本讲提到的 `clang` 作为前端产 IR、`opt`/`llc` 在 IR 上加工，正是三段式设计的具体体现。
- **`opt` 的 `-passes=...` 文本流水线是怎么被解析和执行的？** 这是 [u4-l1 新 Pass 管理器架构](u4-l1-new-pass-manager.md) 的主题；想自己写一个 pass，看 [u4-l4](u4-l4-write-your-own-pass.md)。
- **`llc` 后端那一长串 pass 具体在做什么？** 指令选择、寄存器分配等是整个 [u6 目标代码生成与后端](u6-l1-codegen-overview.md) 单元的内容。
- **`lli`/ORC JIT 内部如何分层？** 见 [u8-l1 ExecutionEngine 与 ORC JIT](u8-l1-executionengine-orc-jit.md)。

建议你现在就先把 [u2-l1](u2-l1-three-phase-design.md) 读一遍，把本讲五个工具「摆」到三段式架构图里，再带着这张图进入 IR 语法的细节学习。
