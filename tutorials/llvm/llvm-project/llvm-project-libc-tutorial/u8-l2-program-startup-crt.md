# 程序启动流程：crt1、do_start 与 TLS

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `crt1.o` 这个启动对象在 LLVM-libc 里**不是一个源文件、而是多个对象文件「可重定位合并」出来的产物**，并列出它的组成部分。
- 画出从「内核把控制权交给 `_start`」到「调用用户 `main`」之间 `do_start` 必须完成的若干关键步骤。
- 解释 TLS（Thread-Local Storage，线程局部存储）是如何从 ELF 的 `PT_TLS` 段被「物化」成一块可用的内存、并把线程指针设置到正确位置的。
- 读懂 `config/app.h` 这个应用描述结构，并指出 `TLSImage` 各字段分别来源于 ELF 程序头的哪个字段。

本讲承接 [u8-l1 OSUtil 与 Linux 系统调用封装](u8-l1-osutil-linux-syscalls.md)：启动过程大量调用 syscall 层（`gettid`、`mmap`、`getrandom`、`arch_prctl`、`exit`），并把那里建立的 `ErrorOr` 错误约定用到极致——因为启动阶段连 `errno` 都还不能用（`errno` 本身就是 TLS 实现的）。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

**ELF 程序头（Program Header）。** 一个 ELF 可执行文件由若干「段（segment）」组成，每个段由一个程序头条目（`ElfW(Phdr)`）描述。本讲关心几类：

| 段类型 | 含义 |
|---|---|
| `PT_PHDR` | 程序头表自身的位置 |
| `PT_DYNAMIC` | 动态链接信息（`_DYNAMIC` 数组） |
| `PT_TLS` | 线程局部存储模板（`.tdata` 初始化数据 + `.tbss` 未初始化数据） |
| `PT_GNU_PROPERTY` | CPU 特性需求（如 CET） |

**入口链（entry chain）。** 程序运行时，内核加载 ELF、设置好栈，然后把指令指针跳到 ELF 的入口符号（通常是 `_start`）。`_start` 之后、`main` 之前这段「无人区」就是 C 运行时（C runtime，crt）的职责：把栈上的 `argc/argv/envp/auxv` 理清楚、把 TLS 搭起来、跑构造函数、最后才调 `main`。传统 glibc 用 `crt1.o`/`crti.o`/`crtn.o` 等一组 `.o` 文件来承担，LLVM-libc 沿用了这套名字，但实现方式很不一样。

**TLS（线程局部存储）。** 用 `_Thread_local`（C11）或 `thread_local`（C++）修饰的变量，每个线程各有一份独立副本。它的物理实现：可执行文件里存一份「模板」（`PT_TLS` 段，其中 `.tdata` 是有初值部分、`.tbss` 是零值部分）；每个线程启动时按模板复制出自己的一份副本，再靠一个「线程指针（thread pointer）」寄存器找到它。x86_64 用 `FS` 段基址（由 `arch_prctl(ARCH_SET_FS)` 设置）作为线程指针。

**load bias（加载偏移）。** 程序在链接时假定一个虚拟地址，运行时被加载到的实际地址可能不同，两者之差就是 `base`（加载偏移）。任何用 `p_vaddr` 算实际地址都要加上 `base`。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|---|---|
| [startup/linux/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/CMakeLists.txt) | 定义「可重定位合并」规则，把多个 `.o` 合成 `crt1.o`；安装启动对象 |
| [startup/linux/x86_64/start.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/start.cpp) | 架构相关的真正入口 `_start`：取栈上的参数、对齐栈、跳到 `do_start` |
| [startup/linux/do_start.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.h) | `do_start` 的声明 |
| [startup/linux/do_start.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp) | **本讲主角**：架构无关的运行时「装配车间」，串起整个启动流程 |
| [startup/linux/x86_64/tls.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp) | x86_64 的 `init_tls` / `set_thread_ptr` / `cleanup_tls` 实现 |
| [config/app.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/app.h) | 按 OS 分派的薄封装，转引 `linux/app.h` 等 |
| [config/linux/app.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/linux/app.h) | 定义 `AppProperties`/`Args`/`TLSImage`/`TLSDescriptor` 等核心结构 |
| [test/integration/startup/linux/](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/test/integration/startup/linux/) | 启动相关集成测试，是本讲实践的依据 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**启动对象**、**do_start**、**TLS 初始化**、**应用描述结构**。四者呈「装配关系」：`crt1.o` 是容器，`_start` 是入口，`do_start` 是中枢，`do_start` 又靠 `app`（应用描述结构）传递状态、靠 `init_tls`（TLS 初始化）搭好线程环境。

### 4.1 启动对象：crt1.o 是怎么拼出来的

#### 4.1.1 概念说明

在传统工具链里，`crt1.o` 是一个提前编译好的目标文件，里面有 `_start`。LLVM-libc 没有这样做：它**不维护一个写死 `crt1.c` 源文件**，而是把启动逻辑拆成多个独立翻译单元（架构相关的 `_start`、架构相关的 TLS 设置、架构无关的 `do_start`、ifunc 重定位、GNU property 段处理），最后在**构建期**用「可重定位合并（relocatable linking）」把它们焊成一个 `crt1.o`。

这样做的好处和入口点机制一脉相承：架构相关与架构无关代码解耦，同一个 `do_start.cpp` 可以服务 x86_64/aarch64/riscv，只需换掉 `.start`/`.tls`/`.irelative` 三个架构组件即可。

#### 4.1.2 核心流程

`crt1.o` 的组装流程：

1. 各组件先各自编译成普通目标文件（`.start.o`、`.tls.o`、`.irelative.o`、`do_start.o`、`gnu_property_section.o`）。
2. `merge_relocatable_object(crt1 ...)` 用 `cc -r -nostdlib` 把它们**可重定位地**合并（`-r` 表示只做部分链接，产出仍是 `.o`，不是可执行文件）。
3. 合并产物 `crt1.o` 被声明成一个 `IMPORTED` 的 `OBJECT` 库目标 `libc.startup.linux.crt1`。
4. `crt1.o`、`crti.o`、`crtn.o` 三个启动对象被安装到库目录，供 Full 模式下手工链接（见 [u1-l3](u1-l3-build-and-run.md) 的 `crt1.o` + `libc.a` 链接示例）。

#### 4.1.3 源码精读

合并规则的核心是 `merge_relocatable_object` 函数。注释解释了为什么要合并：

[startup/linux/CMakeLists.txt:1-9](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/CMakeLists.txt#L1-L9)：一个 crt 对象既有架构相关代码、又有架构无关代码，为减少重复而拆成多个单元，于是需要合并成单个可重定位对象。

[startup/linux/CMakeLists.txt:10-51](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/CMakeLists.txt#L10-L51)：函数体。关键几行——收集各目标的 `$<TARGET_OBJECTS:...>`，用 `add_executable` 建一个临时目标，传 `-r -nostdlib` 做可重定位链接，再声明成 `IMPORTED` 的 `OBJECT` 库。

真正把五个组件焊成 `crt1` 的调用在这里：

[startup/linux/CMakeLists.txt:133-140](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/CMakeLists.txt#L133-L140)：`merge_relocatable_object(crt1 .${ARCH}.start .${ARCH}.tls .${ARCH}.irelative .do_start .gnu_property_section)`——这五行就是 `crt1.o` 的「成分表」。

注意 `crti.o`、`crtn.o` 是单独的、**当前为空**的对象（仓库里 `crti.cpp`/`crtn.cpp` 是 0 字节文件）：

[startup/linux/CMakeLists.txt:142-152](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/CMakeLists.txt#L142-L152)：用 `add_startup_object` 建出 `crti`、`crtn`。它们目前只是占位（传统上 `crti`/`crtn` 负责 `_init`/`_fini` 的框架调用，本实现把 init/fini 数组的遍历直接放进了 `do_start`，所以暂无内容）。

最后三者一起被安装：

[startup/linux/CMakeLists.txt:154-163](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/CMakeLists.txt#L154-L163)：`set(startup_components crt1 crti crtn)`，循环 `install` 这三个对象到库目录。

#### 4.1.4 代码实践

**实践目标：** 确认 `crt1.o` 的「成分表」，并（如已构建）亲眼看到 `_start`、`do_start`、`init_tls` 共存于同一对象。

**操作步骤：**

1. 打开 [startup/linux/CMakeLists.txt:133-140](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/CMakeLists.txt#L133-L140)，列出被合并进 `crt1` 的五个组件，并标注每个是架构相关（`start`/`tls`/`irelative`）还是架构无关（`do_start`/`gnu_property_section`）。
2. 如果你已按 [u1-l3](u1-l3-build-and-run.md) 完成 Full 模式构建，找到构建目录下的 `crt1.o`（一般在 `projects/libc/startup/linux/` 下），运行：

   ```bash
   nm crt1.o | grep -E '_start|do_start|init_tls|set_thread_ptr'
   objdump -d crt1.o | grep -A2 '<_start>'
   ```

**需要观察的现象：** `nm` 应同时列出 `_start`、`do_start`、`init_tls`、`set_thread_ptr` 等符号，证明它们确实被合并进了同一个 `.o`。

**预期结果：** 五个组件的符号都在 `crt1.o` 中；`_start` 的反汇编里能看到对齐栈的 `and` 指令和一个对 `do_start` 的调用。**未运行过构建时，`nm`/`objdump` 的具体输出记为「待本地验证」。**

#### 4.1.5 小练习与答案

**练习 1：** 为什么不直接写一个 `crt1.cpp` 把所有启动逻辑塞进去，而要拆成五个组件再合并？

**参考答案：** 因为启动逻辑里既有架构无关部分（解析栈、遍历程序头、调 `main`——`do_start`），又有强架构相关部分（`_start` 取栈布局的方式、TLS 的 ABI 布局、`set_thread_ptr` 用什么指令）。拆分后 `do_start.cpp` 可跨 x86_64/aarch64/riscv 复用，新增架构只需补 `.start`/`.tls`/`.irelative` 三个组件，符合入口点机制「实现与平台取舍解耦」的设计。

**练习 2：** `crti.cpp`/`crtn.cpp` 当前是 0 字节空文件，为什么它们仍然被安装？

**参考答案：** 它们是传统 crt 套件的占位（`crti.o`/`crtn.o` 通常包裹 `_init`/`_fini`）。本实现把 init/fini 数组的遍历逻辑直接写进了 `do_start`（见 4.2.3），所以暂时不需要内容；但安装它们能让链接命令行（`crt1.o ... crtn.o`）保持与传统习惯一致，也为将来把逻辑迁回 `crti`/`crtn` 留出位置。

---

### 4.2 do_start：libc 运行时的「装配车间」

#### 4.2.1 概念说明

`_start` 很短，因为它只做两件架构相关的事：从栈上取出参数、对齐栈，然后把控制权交给架构无关的 `do_start`。真正「把 libc 运行时搭起来」的全部工作都在 `do_start` 里。可以把 `do_start` 理解成一个**装配车间**：原材料是内核放在栈上的 `argc/argv/envp/auxv` 和 ELF 程序头表，成品是一个「TLS 已就绪、`errno` 可用、构造函数已跑完、可以安全调 `main`」的运行环境。

#### 4.2.2 核心流程

`do_start` 的步骤（顺序很重要）：

```
do_start():
  1. gettid → 记录主线程 tid（失败直接 exit）
  2. 从栈布局解析 envp：argv[argc] 之后是 NULL，再后是 envp[]
     设置全局 environ
  3. 解析 aux-vector（AT_PHDR/AT_PHNUM/AT_PAGESZ/AT_HWCAP/AT_HWCAP2）
  4. 遍历程序头表：
       - 由 PT_PHDR 或 PT_DYNAMIC 算出 load bias (base)
       - 找到 PT_TLS 段
  5. 处理 IRELATIVE 重定位（ifunc 解析器）——若有
  6. 把 PT_TLS 的字段填进 app.tls（address/size/init_size/align）
  7. init_tls(tls) 分配并初始化 TLS 区；set_thread_ptr(tls.tp) 设线程指针
  8. self.attrib = &main_thread_attrib；建立线程 atexit 回调管理器
  9. atexit(call_fini_array_callbacks)  ← 先注册 fini
 10. call_init_array_callbacks(argc, argv, envp)  ← 再跑 init
 11. retval = main(argc, argv, envp)
 12. exit(retval)
```

注意第 9、10 步的**顺序**：先 `atexit(fini)` 再跑 `init_array`。原因是 `init_array` 里的构造函数自己也可能注册 `atexit` 回调，而 C 标准要求「析构与构造反向」——后注册的 atexit 先执行。先把 fini 注册进 atexit 队列，它就会排在用户构造函数注册的回调**之后**执行，从而保证「先析构用户对象，再跑 fini」。集成测试 [init_fini_array_test.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/test/integration/startup/linux/init_fini_array_test.cpp) 正是来验证这套顺序的。

#### 4.2.3 源码精读

先看入口 `_start`（架构相关）如何把栈交给 `do_start`：

[startup/linux/x86_64/start.cpp:11-33](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/start.cpp#L11-L33)：`_start` 用 `__builtin_frame_address(0)` 取栈帧地址。因为本翻译单元用 `-fno-omit-frame-pointer` 编译，函数入口先把旧 `rbp` 压栈，所以栈帧地址 `+1`（一个 `uintptr_t` 字宽）才跳过它、到达内核放的 `argc`。于是 `app.args` 指向 `{argc, argv[]...}`，正好对应 `Args` 结构（见 4.4）。随后两条内联汇编把 `rsp`/`rbp` 对齐到 16 字节（满足 x86_64 ABI），最后调 `do_start()`。

进入 `do_start`。第 1 步记录主线程 id：

[startup/linux/do_start.cpp:69-72](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L69-L72)：`syscall_impl<long>(SYS_gettid)` 取主线程 tid，失败（`<=0`）直接 `SYS_exit(1)`——启动早期没有优雅报错手段。

第 2 步解析环境变量并设置 `environ`：

[startup/linux/do_start.cpp:74-84](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L74-L84)：`argv + argc + 1` 跳过 argv 数组末尾的 NULL，到达 envp；用 `while (*env_end_marker) ++env_end_marker;` 找到 envp 的结尾（下一个 NULL，紧跟着就是 auxv）；最后 `environ = (char**)env_ptr` 把 POSIX 全局变量（《unistd.h》声明的）设上。

第 3、4 步解析 auxv 与程序头表：

[startup/linux/do_start.cpp:88-115](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L88-L115)：先 `auxv::Vector::initialize_unsafe` 把 auxv 起始地址登记进全局缓存（见 u8-l1 的 auxv 工具），再遍历取 `AT_PHDR`（程序头表地址）、`AT_PHNUM`（条目数）、`AT_PAGESZ`（页大小）、`AT_HWCAP`/`AT_HWCAP2`（CPU 特性位）。

[startup/linux/do_start.cpp:117-133](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L117-L133)：遍历程序头表算 `base`（由 `PT_PHDR`：`base = &程序头表 - p_vaddr`；或由 `PT_DYNAMIC`：`base = &_DYNAMIC - p_vaddr`），并记下 `PT_TLS` 段指针。

第 5 步处理 ifunc：

[startup/linux/do_start.cpp:135-139](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L135-L139)：若 `__rela_iplt_start != __rela_iplt_end`（即二进制里有 IRELATIVE 重定位，即 ifunc），调用 `apply_irelative_relocs(base, hwcap, hwcap2)` 提前解析那些「依赖 CPU 特性、运行期才能决定实地址」的函数（典型如 mem* 家族的架构特化分派）。

第 6 步填 TLS 描述（字段来源见 4.4）：

[startup/linux/do_start.cpp:141-144](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L141-L144)：`app.tls.{address,size,init_size,align}` 分别取自 `tls_phdr` 的 `{p_vaddr+base, p_memsz, p_filesz, p_align}`。

第 7 步搭 TLS（详见 4.3）：

[startup/linux/do_start.cpp:148-150](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L148-L150)：`init_tls(tls)` 物化 TLS 区，`set_thread_ptr(tls.tp)` 把线程指针写进 `FS` 基址；若 TLS 非空却设置失败，直接 `SYS_exit(1)`。

第 8 步把主线程登记进线程对象 `self`：

[startup/linux/do_start.cpp:152-154](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L152-L154)：`self.attrib = &main_thread_attrib` 并挂上 atexit 回调管理器。`self` 是定义在 [src/__support/threads/thread.h:250](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/threads/thread.h#L250) 的 `LIBC_THREAD_LOCAL Thread self;`，代表「当前线程」。

第 9、10、11、12 步——收尾与进入 main：

[startup/linux/do_start.cpp:160-170](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L160-L170)：先 `atexit(call_fini_array_callbacks)`，再 `call_init_array_callbacks(...)`（先 `__preinit_array` 后 `__init_array`，见 [do_start.cpp:50-57](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L50-L57)），然后 `main(...)`，最后 `exit(retval)`。`do_start` 标注 `[[noreturn]]`，出口只能经 `exit`。

#### 4.2.4 代码实践

**实践目标：** 用集成测试反向验证 `do_start` 各阶段的正确性。

**操作步骤：**

1. 阅读 [test/integration/startup/linux/args_test.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/test/integration/startup/linux/args_test.cpp)。它的 `TEST_MAIN` 断言 `argc == 4`、`argv[1..3] == "1","2","3"`，并扫描 envp 找 `FRANCE=Paris`、`GERMANY=Berlin`。把这对应到 `do_start` 的第 2 步（envp 解析）——如果 `argv + argc + 1` 算错，envp 扫描就会失败。
2. 阅读 [test/integration/startup/linux/init_fini_array_test.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/test/integration/startup/linux/init_fini_array_test.cpp)。注意它用了 `__attribute__((constructor))`、`__attribute__((destructor(1)/destructor(2)))` 和手写 `.preinit_array` 段。两个 `destructor` 都断言 `global_destroyed == true` 且彼此的初值已被正确设置/清除。
3. 在构建后用 `ninja libc-loader-test` 或对应的 integration 目标运行（具体目标名以构建系统为准，**记为「待本地验证」**）。

**需要观察的现象：** `init_fini_array_test` 通过，证明 `preinit_array → init_array → main → (用户析构) → fini_array` 的顺序正确，特别是「析构反向于构造」。

**预期结果：** 三个测试（args/tls/init_fini）全绿，分别覆盖 `do_start` 的参数解析、TLS、init/fini 三大职责。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `do_start` 处理 IRELATIVE 重定位（第 5 步）必须在 `init_tls`/`set_thread_ptr`（第 7 步）之前？难道 ifunc 解析器会用到 TLS？

**参考答案：** 时序上第 5 步确实在第 7 步之前，但更关键的原因是：ifunc 解析器（resolver）可能调用任意 libc 函数，而这些函数可能依赖 TLS（如 `errno`）。不过当前实现的顺序主要服从「先把地址算清楚（base、ifunc）、再搭运行时」的自然依赖；此外 ifunc resolver 在很多实现里会被 `__libc_start_main` 之后再次调用，本实现选择在 TLS 之前先做，是为了让随后的所有代码（包括 init_array）都能安全引用已被解析的真实函数地址。这是一个实现取舍点，值得在阅读时与社区讨论对照。

**练习 2：** 如果删掉第 9 步的 `atexit(call_fini_array_callbacks)`、只保留第 10 步的 init_array，会发生什么？

**参考答案：** 程序仍能进入 `main` 并正常运行，但退出时 `__fini_array` 里的析构回调（包括 `__attribute__((destructor))` 标注的函数、C++ 全局对象析构）将不会被执行。`init_fini_array_test.cpp` 中的 `global_destroyed` 就不会被置 true，相关断言会失败。

---

### 4.3 TLS 初始化：从 PT_TLS 到线程指针

#### 4.3.1 概念说明

`do_start` 第 6 步只是把 `PT_TLS` 段的**元信息**抄进 `app.tls`，真正「按模板造出一份可用的 TLS 副本、并把线程指针指向它」的工作在第 7 步的 `init_tls` + `set_thread_ptr` 里，由架构相关文件 [startup/linux/x86_64/tls.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp) 完成。

为什么这一步如此关键、又如此棘手？因为**一旦 TLS 没搭好，任何依赖 TLS 的设施都不可用**——而 LLVM-libc 的 `errno` 恰恰是用 TLS 实现的（见 [u4-l3](u4-l3-error-handling-errno.md)）。所以 `init_tls` 自己**绝对不能调用任何会触达 `errno` 的函数**，它只能用裸 syscall（`mmap`/`getrandom` 经由返回 `ErrorOr` 的薄封装）。

#### 4.3.2 核心流程

x86_64 的 TLS 布局遵循其 ABI：**线程指针指向 TLS 块的末尾，TLS 从线程指针向低地址（向下）生长**，且线程指针所指的位置还存着一个「指向 TLS 块首字节」的自指针。这块内存还要额外容纳栈金丝雀（stack canary，位于线程指针偏移 `0x28` = 40 字节处）。

`init_tls` 的步骤：

1. 若 `app.tls.size == 0`（程序没有 TLS），直接置空描述符返回。
2. 把 `size` 向上取整到 `align` 的倍数（`align` 是 2 的幂）。
3. 在整大小基础上再加一个字（自指针）和 40 字节（栈金丝雀区），得到 `tls_size_with_addr`。
4. `mmap` 一块匿名可读写内存。
5. 在块末尾写入自指针（x86_64 ABI 要求）。
6. 用 `inline_memcpy` 把 `app.tls.init_size` 字节的 `.tdata` 初值从镜像拷进块首。
7. 用 `getrandom` 给栈金丝雀填一个随机值（不能调会设 errno 的 `get_random`）。
8. 填好 `TLSDescriptor{size, addr, tp=end_ptr}` 返回；随后 `set_thread_ptr` 用 `arch_prctl(ARCH_SET_FS, tp)` 把 `FS` 基址设成 `tp`。

向上取整对齐的算法值得用公式说清。设 `s = app.tls.size`、`a = app.tls.align`（2 的幂），代码用：

\[
\text{tls\_size} = (s\ \&\ (-a))
\]

因为 `a` 是 2 的幂，`-a` 的二进制是「低 `log2(a)` 位为 0、其余为 1」的掩码，所以 `s & (-a)` 把 `s` 向**下**取整到 `a` 的倍数；若结果小于 `s`，就再加一个 `a`，从而等价于向上取整：

\[
\text{tls\_size} = \lceil s / a \rceil \cdot a
\]

#### 4.3.3 源码精读

[startup/linux/x86_64/tls.cpp:23-71](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp#L23-L71)：`init_tls` 全函数体。

逐段看关键点：

- [tls.cpp:24-28](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp#L24-L28)：无 TLS 时早退。
- [tls.cpp:30-33](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp#L30-L33)：上述向上取整对齐。
- [tls.cpp:35-40](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp#L35-L40)：注释解释「线程指针指向 TLS 块地址」+ 额外留 `sizeof(uintptr_t) + 40` 给自指针和偏移 `0x28` 的栈金丝雀。
- [tls.cpp:42-47](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp#L42-L47)：`linux_syscalls::mmap`（返回 `ErrorOr`，失败则 `SYS_exit(1)`）。
- [tls.cpp:49-52](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp#L49-L52)：x86_64 TLS 「向下生长」，`end_ptr = 块基址 + tls_size`，在 `end_ptr` 处写入 `end_ptr` 自身（自指针）——这就是线程指针最终要指向的地方。
- [tls.cpp:54-56](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp#L54-L56)：用 `inline_memcpy`（来自 mem* 框架，见 [u5-l2](u5-l2-mem-framework-and-arch.md)）把 `.tdata` 初值从 `app.tls.address` 拷到块首，长度为 `app.tls.init_size`。
- [tls.cpp:57-66](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp#L57-L66)：**关键注释**——不能用 `get_random`，因为它失败时会设 `errno`，而 `errno` 是 TLS 变量、此刻还没搭好；故改用返回 `ErrorOr` 的 `linux_syscalls::getrandom`。金丝雀写在 `end_ptr + 40`（即 `%fs:0x28`）。
- [tls.cpp:68-69](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp#L68-L69)：填充 `TLSDescriptor = {tls_size_with_addr, 块基址, end_ptr}`。

设置线程指针：

[startup/linux/x86_64/tls.cpp:80-82](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp#L80-L82)：`set_thread_ptr` 调 `syscall_impl(SYS_arch_prctl, ARCH_SET_FS, val)`，把 `FS` 段基址设成线程指针 `tp`。自此 `%fs:0` 起就是 TLS 区。

这套 TLS 是否真的能用？集成测试 [tls_test.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/test/integration/startup/linux/tls_test.cpp) 给出了端到端证据：

[test/integration/startup/linux/tls_test.cpp:16-39](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/test/integration/startup/linux/tls_test.cpp#L16-L39)：声明 `_Thread_local int a[101] = {123}`，在 `main` 里读写所有元素，再故意用一个错误 `mmap` 触发 `errno`，断言 `errno == EINVAL`——这同时验证了 TLS 副本正确（`a[0]==123`）和「`errno` 经 TLS 可读写」。

#### 4.3.4 代码实践

**实践目标：** 在纸上还原 `init_tls` 产出的内存布局。

**操作步骤：**

1. 设想一个程序：`app.tls.size = 30`，`app.tls.init_size = 30`，`app.tls.align = 16`，`sizeof(uintptr_t) = 8`。
2. 手算：取整后的 `tls_size = 32`；`tls_size_with_addr = 32 + 8 + 40 = 80`；`mmap` 返回块基址 `B`；`end_ptr = B + 32`。
3. 画出从 `B` 到 `B + 80` 的内存图，标出：`.tdata` 拷贝区（`B..B+30`）、对齐填充（`B+30..B+32`）、自指针（`end_ptr = B+32`，8 字节）、40 字节区、栈金丝雀（`end_ptr+40 = B+72`，8 字节）。
4. 指出 `set_thread_ptr` 把 `FS` 设成 `end_ptr = B+32`，于是用户代码里的 `_Thread_local` 变量地址 = `FS - (其相对 TLS 块尾的偏移)`。

**需要观察的现象：** 自指针落在 `FS` 所指处，金丝雀落在 `FS + 0x28`，与代码完全对应。

**预期结果：** 布局图与 [tls.cpp:35-69](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp#L35-L69) 的注释与赋值一一吻合。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `init_tls` 用 `inline_memcpy` 而不是公开的 `memcpy`？

**参考答案：** 公开 `memcpy` 是入口点，其实现路径上可能依赖已初始化的运行时（且符号名经 `LLVM_LIBC_FUNCTION` 映射、命名空间隔离）；更重要的是启动阶段应尽量自包含、可预测。`inline_memcpy`（mem* 框架的内联版）是一个不依赖运行时的内部工具，适合在 TLS 尚未完全就绪时使用。

**练习 2：** 如果把金丝雀那段的 `getrandom` 换成调用公开的 `getrandom`（会设 `errno`），会在什么时候出问题？

**参考答案：** 此刻 `set_thread_ptr` 尚未执行，`FS` 还指向别处，`errno`（TLS 变量）的存储位置未建立；写 `errno` 会写到错误地址，可能破坏内存或读到垃圾值。这正是代码注释强调「不能用会设 errno 的函数」的原因。

---

### 4.4 应用描述结构：config/app.h 与 AppProperties

#### 4.4.1 概念说明

`do_start` 与架构相关代码（`_start`、`init_tls`）之间需要一个**共享的数据载体**来传递「这个应用长什么样」——栈参数在哪、TLS 镜像在哪、页大小是多少。这个载体就是全局变量 `app`，类型 `AppProperties`，定义在 [config/linux/app.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/linux/app.h)。它放在 `config/` 而不是 `src/`，因为它是「平台相关、但不对应任何公共头文件」的内部约定，由 `add_header_library(app_h ...)` 注册为内部头库（见 [config/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/CMakeLists.txt)）。

#### 4.4.2 核心流程

应用描述结构的数据流：

```
内核 → 栈上的 argc/argv/envp/auxv
           │
           ▼  (_start 把栈帧地址 +1 赋给 app.args)
       app.args  ──► Args { argc, argv[] }
           │
           ▼  (do_start 解析 auxv、程序头)
       app.page_size   ◄── AT_PAGESZ
       app.env_ptr     ◄── envp 起始
       app.tls         ◄── PT_TLS: {address, size, init_size, align}
           │
           ▼  (init_tls 据此物化 TLS 区)
       TLSDescriptor { size, addr, tp } ──► set_thread_ptr(tp)
```

注意 `config/app.h` 是个按 OS 分派的薄封装，同一句 `#include "config/app.h"` 在不同目标上拿到不同 OS 的定义：

[config/app.h:14-20](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/app.h#L14-L20)：按 `LIBC_TARGET_ARCH_IS_GPU`/`__linux__`/`__UEFI__` 分别转引 `gpu/app.h`、`linux/app.h`、`uefi/app.h`。这与 [u8-l1](u8-l1-osutil-linux-syscalls.md) 的「目录隔离 + 头文件分派」是同一套思路。

#### 4.4.3 源码精读

`TLSImage`——描述 TLS 镜像的四个字段，正好对应 `PT_TLS` 程序头：

[config/linux/app.h:19-36](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/linux/app.h#L19-L36)：
- `address`：TLS 的加载地址；
- `size`：`.tdata + .tbss` 的总大小，即 `PT_TLS` 的 `p_memsz`；
- `init_size`：有初值部分（`.tdata`）的大小，即 `p_filesz`；
- `align`：对齐（2 的幂），即 `p_align`。

`Args`——栈参数的「柔性数组」伪装：

[config/linux/app.h:38-48](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/linux/app.h#L38-L48)：`{ uintptr_t argc; uintptr_t argv[1]; }`。注释说明 C++ 没有柔性数组（P1039 提案想修），所以用 `argv[1]` 假装；`argv[1]` 长度足够，因为 `argc` 至少为 1，且 argv 数组末尾必有 8 字节 NULL。

`AppProperties`——把上述拼成「应用画像」，并声明全局 `app`：

[config/linux/app.h:51-64](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/linux/app.h#L51-L64)：`{ page_size; Args *args; TLSImage tls; uintptr_t *env_ptr; }`，外加 `[[gnu::weak]] extern AppProperties app;`。实际定义在 [do_start.cpp:45](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L45) 的 `AppProperties app;`。`weak` 属性允许在没有 crt 的场景（如被别的运行时托管）下提供替代定义。

`TLSDescriptor`——每个线程 TLS 区的运行期描述，供 `init_tls` 输出：

[config/linux/app.h:67-79](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/linux/app.h#L67-L79)：`{ size; addr; tp; }`，其中 `tp` 是「线程指针寄存器应被设置的值」，注释强调它依架构 ABI 可能等于 `addr` 也可能不是（x86_64 上它等于 `end_ptr`，并非块基址）。

三个函数声明——把架构相关实现与该头解耦：

[config/linux/app.h:83-89](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/linux/app.h#L83-L89)：`init_tls`、`cleanup_tls`、`set_thread_ptr` 只声明，实现在各架构的 `tls.cpp`（如 [x86_64/tls.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/x86_64/tls.cpp)）。

最后，`app.tls` 各字段的**来源**就在 `do_start` 里：

[startup/linux/do_start.cpp:141-144](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L141-L144)：`address = tls_phdr->p_vaddr + base`、`size = p_memsz`、`init_size = p_filesz`、`align = p_align`。

#### 4.4.4 代码实践（本讲主实践任务）

**实践目标：** 对照 `do_start.h` 与 `config/linux/app.h`，回答两个问题——(A) 写出「从内核交接到 `main`」之间 `do_start` 至少要完成的三件事；(B) 指出 `TLSImage` 各字段的来源。

**操作步骤：**

1. 打开 [startup/linux/do_start.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.h)，确认 `do_start` 的契约：`[[noreturn]] void do_start();`——无参、不返回，所有状态经全局 `app` 传递。
2. 打开 [config/linux/app.h:19-36](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/linux/app.h#L19-L36) 与 [do_start.cpp:141-144](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L141-L144)，把每个 `TLSImage` 字段与 `PT_TLS` 程序头字段一一对应。

**需要观察的现象 / 预期结果：**

(A) 三件事（参考答案）：

1. **认清运行环境**：从栈布局解析出 `argv/envp`（设全局 `environ`），从 auxv 取页大小、程序头表，从程序头算出加载偏移 `base`、定位 `PT_TLS`。
2. **搭好线程运行时**：依据 `PT_TLS` 物化 TLS 区（`init_tls`）、设置线程指针（`set_thread_ptr`），使 `errno` 等 TLS 设施可用；登记主线程到 `self`。
3. **跑构造、注册析构、进入 main**：`atexit(fini)` → `init_array` → `main` → `exit`。

(B) `TLSImage` 字段来源（参考答案）：

| `TLSImage` 字段 | 来源 | 填充位置 |
|---|---|---|
| `address` | `PT_TLS` 的 `p_vaddr` + 加载偏移 `base` | [do_start.cpp:141](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L141) |
| `size` | `PT_TLS` 的 `p_memsz`（= `.tdata` + `.tbss`） | [do_start.cpp:142](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L142) |
| `init_size` | `PT_TLS` 的 `p_filesz`（= `.tdata`） | [do_start.cpp:143](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L143) |
| `align` | `PT_TLS` 的 `p_align` | [do_start.cpp:144](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L144) |

#### 4.4.5 小练习与答案

**练习 1：** `extern AppProperties app` 声明为 `[[gnu::weak]]`，有什么好处？

**参考答案：** `weak` 让链接器允许多处定义、选其一。这样当 LLVM-libc 被别的运行时托管、或在不同启动路径下，可以由那一侧提供 `app` 的定义而不会与 `do_start.cpp` 里的定义冲突；同时也让没有完整 crt 的目标可以省略它。

**练习 2：** 为什么 `Args::argv` 用 `argv[1]` 而不是 `argv[]`（C 的柔性数组）或 `*argv`？

**参考答案：** C++ 标准没有柔性数组（C99 才有，且 C++ 至今靠扩展支持），`argv[1]` 是「假装柔性数组」的惯用法：它给出一个合法的下标 0 元素，运行期实际访问 `argv[0..argc]` 时只是越过数组名义边界——这在底层是安全的，因为 `app.args` 实际指向栈上连续的 `argc` + 若干指针。用 `*argv` 则丢失了「后面紧跟着一组指针」的意图表达。

---

## 5. 综合实践

**任务：** 画一张「内核 → `main`」的完整启动时序图，并做一次「破坏性预测」。

**步骤：**

1. 在一张图上横向排出四个角色：**内核** → **`_start`（crt1.o，架构相关）** → **`do_start`（架构无关）** → **`main`**。
2. 在 `_start` 与 `do_start` 之间的箭头上标注传递物：`app.args`（栈帧地址 +1）。
3. 在 `do_start` 内部纵向标出本讲讲的全部阶段（gettid → 解析 envp/auxv → 程序头/base/PT_TLS → irelative → 填 `app.tls` → `init_tls`/`set_thread_ptr` → `self.attrib`/atexit 管理 → `atexit(fini)` → init_array → main → exit），并在每个阶段旁标注它依赖的全局/符号（如 `init_tls` 依赖 `app.tls`、`environ`、auxv 缓存）。
4. 标出「TLS 就绪」这条分界线：在此**之前**不能调用任何会触达 `errno` 的函数；在此**之后**才可以。
5. **破坏性预测：** 假设把 `set_thread_ptr(tls.tp)`（[do_start.cpp:149](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/do_start.cpp#L149)）注释掉，但保留 `init_tls`。预测：(a) `_Thread_local` 变量读写会怎样？(b) 一次会失败的 syscall（如 `tls_test.cpp` 里的坏 `mmap`）触发的 `errno` 写入会落到哪里？(c) `init_fini_array_test.cpp` 还能通过吗？

**参考答案要点：** (a) `FS` 仍是旧值，`_Thread_local` 变量地址算错，读到/写到随机内存，`tls_test.cpp` 的 `a[0]==123` 大概率失败或段错误；(b) `errno`（TLS 变量）的写地址基于错误 `FS`，会写到未映射或他人内存，可能直接段错误而非设 `EINVAL`；(c) init/fini 数组本身不依赖 TLS，`main` 仍能进入，但其内任何触达 `errno`/TLS 的断言会崩，整体难以通过。把预测与你（若有构建环境）实际注释后跑测试的结果对照，**实际运行结果记为「待本地验证」**。

## 6. 本讲小结

- `crt1.o` 不是单一源文件，而是 `start`/`tls`/`irelative`/`do_start`/`gnu_property_section` 五个组件经 `merge_relocatable_object`（`cc -r`）在构建期焊成的可重定位对象；`crti.o`/`crtn.o` 当前为空占位。
- `_start`（架构相关）只负责取栈参数、对齐栈，然后跳进架构无关的 `do_start`；二者经全局 `app` 传递状态。
- `do_start` 是运行时「装配车间」：解析栈与 auxv、算加载偏移、定位 `PT_TLS`、处理 ifunc、搭 TLS、登记主线程、`atexit(fini)` 后跑 `init_array`、最后 `main` 与 `exit`。
- TLS 由架构相关 `init_tls` 物化：`mmap` 一块、写入自指针、拷贝 `.tdata` 初值、填随机栈金丝雀，再用 `set_thread_ptr`（x86_64 上 `arch_prctl(ARCH_SET_FS)`）设置线程指针。
- 启动早期「TLS 就绪线」之前不能用任何会设 `errno` 的函数（因为 `errno` 本身是 TLS），故 `init_tls` 全程用裸 syscall 的 `ErrorOr` 薄封装。
- `AppProperties`/`TLSImage`/`Args`/`TLSDescriptor` 定义在 `config/<os>/app.h`，由 `config/app.h` 按 OS 分派；`TLSImage` 四字段直接来源于 `PT_TLS` 的 `p_vaddr`/`p_memsz`/`p_filesz`/`p_align`。

## 7. 下一步学习建议

- 继续向「系统交互」深入：[u8-l3 stdio FILE 模型与文件 I/O](u8-l3-stdio-file-model.md) 讲解 `FILE` 抽象如何建立在已就绪的运行时（含 TLS/errno）之上。
- 若对并发感兴趣，可跳到 [u9-l2 线程与同步原语](u9-l2-threads-synchronization.md)，对比「主线程由 `do_start` 登记」与「普通线程由 `pthread_create` 走 `Thread` 对象」两条 TLS 初始化路径的异同。
- 建议延伸阅读源码：[startup/linux/aarch64/tls.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/aarch64/tls.cpp) 与 [startup/linux/riscv/tls.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/startup/linux/riscv/tls.cpp)，体会不同架构 ABI（线程指针指向块首还是块尾）如何只换 `init_tls`/`set_thread_ptr` 而保持 `do_start` 不变。
