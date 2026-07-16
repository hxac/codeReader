# GDB 远程调试与 LLDB

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 **GDB 远程串行协议（GDB Remote Serial Protocol）** 的包格式与命令分派原理，并能读懂模拟器里收发包的源码。
- 解释模拟器的 **gdb 模式** 是如何启动、监听 8000 端口并在主循环里逐条响应宿主机调试器命令的。
- 理解 **断点（breakpoint）** 与 **单步（single step）** 在指令集模拟器里的实现：用一条“非法指令”替换原指令、命中后如何停止、继续时如何“跨过”断点。
- 掌握用 `-m gdb` 启动模拟器、用 LLDB `gdb-remote 8000` 附着、设置断点、单步、读写寄存器与内存的完整流程。
- 明确这套调试机制在 **启用虚拟内存** 时的硬性限制及其根因。

## 2. 前置知识

本讲是“调试与性能”单元的第三篇，承接两个前置讲义：

- **u8-l1 模拟器架构与指令执行**：我们已经知道 Nyuzi 的 C 指令集模拟器（ISS）只维护**架构状态**（标量/向量寄存器、平坦内存数组、PC、控制寄存器、TLB），不建模流水线与缓存；一条指令就是一次 `execute_instruction` 调用；模拟器有 normal / cosim / gdb 三种模式。本讲的“gdb 模式”正是这第三种模式。
- **u11-l1 片上调试器与 JTAG**：那是**硬件侧**的调试通道（通过 JTAG 注入指令、经 `CR_JTAG_DATA` 信箱搬数据）。本讲则是**模拟器侧**的调试通道——因为模拟器本身就是一段 C 程序，它不需要 JTAG，而是直接把内部的架构状态通过一个 TCP socket 暴露给宿主机上的调试器。

几个需要先建立的术语：

- **宿主机（host）**：运行调试器（LLDB/GDB）的那台机器。
- **目标机（target）**：被调试的对象。本讲里目标机就是模拟器进程，它扮演“调试桩（debug stub）”的角色。
- **GDB 远程串行协议**：宿主机调试器与调试桩之间的一套文本协议。GDB 和 LLDB 都会说这套协议，所以一个写得合格的桩可以同时被两者驱动。本讲的 `remote-gdb.c` 主要面向 LLDB 调试。
- **SIGTRAP**：POSIX 信号 5，调试器约定用它表示“程序因断点或单步而暂停”。模拟器里用常量 `TRAP_SIGNAL = 5` 表示它。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tools/emulator/remote-gdb.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c) | GDB 远程协议的调试桩主体：建 socket、收发包、分派命令、调用底层 `dbg_*` 接口。 |
| [tools/emulator/remote-gdb.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.h) | 只导出一个函数 `remote_gdb_main_loop`。 |
| [tools/emulator/main.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c) | 模拟器入口：解析 `-m gdb`、在 gdb 模式下打开 `stop_on_fault` 并进入 `remote_gdb_main_loop`。 |
| [tools/emulator/processor.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c) | 调试桩依赖的底层接口都在这里：`dbg_get_pc` / `dbg_single_step` / 读写寄存器 / 读写内存 / 设清断点，以及断点命中检测。 |
| [tools/emulator/processor.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.h) | 上述 `dbg_*` 接口的声明。 |
| [tools/emulator/util.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/util.h) | 收发包用到的 `endian_swap32`、`can_read_file_descriptor`、`parse_hex_vector`。 |
| [tools/emulator/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/README.md) | 官方的 LLDB 调试步骤与已知限制。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 远程协议**（包格式与收发）、**4.2 gdb 模式**（启动与命令分派）、**4.3 断点**（指令替换与命中）、**4.4 单步与寄存器/内存访问**。后两个共同对应“断点单步”这一最小模块。

### 4.1 GDB 远程串行协议：包格式与收发

#### 4.1.1 概念说明

调试器（宿主）和被调试程序（目标）往往不在同一个进程里，甚至不在同一台机器上。GDB Remote Serial Protocol 把两者之间所有的通信都规范成一种** ASCII 文本包**：宿主发一条命令包，目标回一个应答包。这套协议原本是为串口设计的，但如今几乎都跑在 TCP 上——模拟器正是开一个 TCP 监听端口（8000）来扮演目标。

每个包的格式是：

```
$<命令内容>#<两位十六进制校验和>
```

- `$` 是包起始符。
- `<命令内容>` 是一串 ASCII 字符，例如 `g`（读寄存器）、`m1000,4`（从地址 0x1000 读 4 字节）、`Z0,1000,4`（在 0x1000 设断点）。
- `#` 是包结束符。
- `<校验和>` 是命令内容每个字节的 ASCII 值之和对 256 取模，写成两位十六进制。

接收方每收到一个包，要回送一个**单字符确认**：`+` 表示校验通过（ACK），`-` 表示出错要求重发（NACK）。为了减少开销，LLDB 会用 `QStartNoAckMode` 协商进入“无确认模式”，此后双方不再发 `+`。

#### 4.1.2 核心流程

```
宿主 LLDB                        模拟器调试桩
   |  $<cmd>#<cksum>   ────────►  |
   |                               | 校验和校验（此处简化为直接信任）
   |  ◄──────────────  +          | 回 ACK
   |                               | 分派 cmd，执行
   |  $<resp>#<cksum>  ◄────────  | 回送应答包
   |  ────────►  +                | 宿主 ACK
```

读包（`read_packet`）的算法：

1. 不断读字节，直到看见 `$`（丢掉包之前的杂散字节，例如宿主发的中断 `Ctrl-C`）。
2. 继续读字节存入缓冲区，直到看见 `#`。
3. 再读两个字节作为校验和，**直接丢弃**（本桩不校验，简化实现）。

发包（`send_response_packet`）的算法：

1. 依次写 `$`、应答正文、`#`。
2. 累加正文字节得到校验和，写成两位十六进制追加在末尾。

#### 4.1.3 源码精读

读包逻辑，先找起始符 `$`、再读正文到 `#`、最后丢弃两位校验和：[tools/emulator/remote-gdb.c:56-96](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L56-L96)。注意第 86–87 行两次 `read_byte()` 就是把校验和读出来扔掉。

发包逻辑，写 `$正文#`，再算校验和并补两位 hex：[tools/emulator/remote-gdb.c:98-127](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L98-L127)。校验和就是第 117–119 行那个逐字节累加的循环。

十六进制字节解码（内存写入时要把宿主发来的 hex 串还原成字节）在 [tools/emulator/remote-gdb.c:159-177](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L159-L177)。

一个常量贯穿全文件：`TRAP_SIGNAL` 固定为 5（SIGTRAP），断点命中或单步完成后用它告诉宿主“程序因陷阱暂停”——[tools/emulator/remote-gdb.c:35](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L35)。

#### 4.1.4 代码实践

**目标**：亲眼看到协议包的真实模样。

**步骤**：

1. 打开 [tools/emulator/remote-gdb.c:33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L33)，把 `#define LOG_COMMANDS 0` 改成 `1`，重新 `make` 模拟器（**这只是本地观察用的改动，不要提交；也可不改源码、改用下面的“源码阅读型”替代**）。
2. 用 `bin/nyuzi_emulator -m gdb <program>.hex` 启动模拟器（程序需带 `-g` 调试信息编译）。
3. 另开终端，按 README 用 LLDB 附着：`/usr/local/llvm-nyuzi/bin/lldb --arch nyuzi <program>.elf -o "gdb-remote 8000"`。
4. 在 LLDB 里输入 `(lldb) continue`。

**需要观察的现象**：模拟器终端会逐行打印形如 `GDB recv: vCont;c:0001` 与 `GDB send: S05` 的日志。

**预期结果**：能看到 `$...#` 的包体被剥去外壳后的命令字符串，以及应答 `S05`（信号 5，即 SIGTRAP）。把打印出的 `recv` 与本讲后面的命令表逐条对上。**若你没有本地工具链，则改为“源码阅读型实践”**：对照 [tools/emulator/remote-gdb.c:56-96](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L56-L96) 手工模拟一次 `read_packet`，输入 `$qHostInfo#0d`，写出缓冲区里最终留下的字符串与被丢弃的两个字节。**运行结果待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：宿主发来 `$m1000,4#`，校验和应该是多少（忽略实际值，说明算法）？
**答案**：把命令内容 `"m1000,4"` 每个字符的 ASCII 码相加（`m`=109, `1`=49, `0`=48×3, `,`=44, `4`=52），求和后对 256 取模，写成两位十六进制。

**练习 2**：为什么协议里要有 `+`/`-` 确认机制，而本桩又提供 `QStartNoAckMode`？
**答案**：原始协议跑在可能丢字节、错位的串口上，确认与重发保证可靠交付；改跑 TCP 后字节流本身可靠，逐包确认纯属开销，故 LLDB 协商关闭它以提速。

---

### 4.2 模拟器的 gdb 模式：启动与命令分派

#### 4.2.1 概念说明

“gdb 模式”是模拟器三种运行模式之一（normal / cosim / gdb）。它和前两种的本质区别是：**主循环不再“埋头跑到程序结束”，而是变成一个“命令服务台”**——监听端口、接受一个调试器连接、然后在 `while` 循环里不断读包、分派、回包，直到收到 `k`（kill）命令才退出。

另一个关键差异：gdb 模式下，模拟器会打开 `stop_on_fault` 开关。在 normal 模式下，程序发生某些异常会被派发给 trap 处理例程或直接报告崩溃；而在 gdb 模式下，调试器希望**程序一发生可观察的停止事件就暂停**，好让人去查现场，所以要把这些异常转化为“停下来”。

#### 4.2.2 核心流程

```
main.c
 ├─ getopt 解析 -m gdb  → mode = MODE_GDB_REMOTE_DEBUG
 ├─ 加载 hex 镜像、初始化设备
 └─ switch(mode):
      case MODE_GDB_REMOTE_DEBUG:
         dbg_set_stop_on_fault(proc, true)     // 让异常停下而非派发
         remote_gdb_main_loop(proc, ...)        // 进入命令服务台

remote_gdb_main_loop:
  1. 在 8000 端口建 TCP 监听 socket（SO_REUSEADDR）
  2. 外层 while：accept 一个调试器连接
  3. 内层 while：read_packet → 按 request[0] 分派命令 → send_response_packet
  4. 收到 'k' → return，结束整个会话
```

每条命令分派到 4.3、4.4 两节展开的底层 `dbg_*` 接口。命令到接口的对应关系（只列重点）：

| 协议命令 | 含义 | 桩的动作 |
|----------|------|----------|
| `c` / `C` | continue（继续运行） | `run_until_interrupt` 跑到断点或宿主中断，回 `S05` |
| `s` / `S` | 单步一条指令 | `dbg_single_step`，回 `S05` |
| `vCont;...` | continue/step（LLDB 常用） | 解析后等价于上面两种 |
| `m addr,len` | 读内存 | `dbg_read_memory_byte` 逐字节转 hex |
| `M addr,len:xx...` | 写内存 | `decode_hex_byte` + `dbg_write_memory_byte` |
| `p reg` / `g` | 读一个寄存器 | `dbg_get_scalar_reg` / `dbg_get_vector_reg` / `dbg_get_pc` |
| `G reg=val` | 写一个寄存器 | `dbg_set_scalar_reg` / `dbg_set_vector_reg` |
| `Z0,addr,4` | 设断点 | `dbg_set_breakpoint` |
| `z0,addr,4` | 清断点 | `dbg_clear_breakpoint` |
| `H` / `qC` | 选/查当前线程 | 维护 `current_thread`（0 基） |
| `qfThreadInfo` | 枚举线程 | 返回 `m1,2,...,N` |
| `qHostInfo` | 目标信息 | `triple:nyuzi;endian:little;ptrsize:4` |
| `qRegisterInfo N` | 寄存器元信息 | 返回名字/位宽/generic 角色，供 LLDB 画寄存器面板 |
| `QStartNoAckMode` | 关闭确认 | 置 `no_ack_mode=true` |
| `?` | 查停止原因 | 回当前线程的 `last_signals` |
| `k` | 杀掉 | `return` 退出主循环 |

#### 4.2.3 源码精读

`main.c` 里模式枚举与 `-m` 解析：[tools/emulator/main.c:165-170](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L165-L170) 定义三态枚举，[tools/emulator/main.c:197-210](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L197-L210) 把字符串 `"gdb"` 映射到 `MODE_GDB_REMOTE_DEBUG`。注意 normal 与 cosim 两种模式都调用 `dbg_set_stop_on_fault(proc, false)`，唯独 gdb 模式为 `true`——见 [tools/emulator/main.c:429-432](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L429-L432)。

> 这是“异常如何变成暂停”的入口：`stop_on_fault` 让处理器在 fault 发生时不再派发给 trap 例程，而是停住，等调试器来读现场，详见 [tools/emulator/processor.c:558-561](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L558-L561) 与 processor.c 内部对它的判断。

调试桩主体 `remote_gdb_main_loop`：[tools/emulator/remote-gdb.c:179-547](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L179-L547)。

- 建 socket、绑定、监听 8000 端口：[tools/emulator/remote-gdb.c:196-223](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L196-L223)（端口常量在第 211 行 `htons(8000)`）。`SO_REUSEADDR` 让模拟器重启后能立刻重新绑定。
- 外层 `accept` 循环与内层命令循环：[tools/emulator/remote-gdb.c:225-243](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L225-L243)。
- 收到包后，若未进入 no-ack 模式则回 `+`：[tools/emulator/remote-gdb.c:246-253](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L246-L253)。
- 巨大的 `switch (request[0])` 命令分派：[tools/emulator/remote-gdb.c:255-542](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L255-L542)。
- continue 的实现 `run_until_interrupt`：[tools/emulator/remote-gdb.c:139-157](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L139-L157)。它一批批地调 `execute_instructions(proc, screen_refresh_rate)`，只要这函数返回假（命中断点，见 4.3）就跳出；同时每批之间用 `can_read_file_descriptor(client_socket)` 检查宿主有没有发来中断（LLDB 里按 Ctrl-C 会向 socket 写一个字节），有就也跳出。

一个为 LLDB 量身定做的细节：`qHostInfo` 返回 `triple:nyuzi;endian:little;ptrsize:4`，`qRegisterInfo` 逐个返回寄存器的名字/位宽/generic 角色——这两个是 **LLDB 对 GDB 协议的扩展**（GDB 用 `qSupported` + target.xml，LLDB 用 `qRegisterInfo`），见 [tools/emulator/remote-gdb.c:412-413](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L412-L413) 与 [tools/emulator/remote-gdb.c:434-457](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L434-L457)。这也是为什么 README 标题是“Debugging with LLDB”。

#### 4.2.4 代码实践

**目标**：跑通“模拟器 gdb 模式 + LLDB 附着”这条官方链路。

**步骤**（来自 [tools/emulator/README.md:62-77](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/README.md#L62-L77)）：

1. 终端 A 启动模拟器：`bin/nyuzi_emulator -m gdb <program>.hex`（程序要带 `-g` 编译，关掉优化更佳）。
2. 终端 B（在编译程序的那个目录，以便找到源码）：
   ```
   /usr/local/llvm-nyuzi/bin/lldb --arch nyuzi <program>.elf -o "gdb-remote 8000"
   ```
3. 进入 LLDB 提示符后输入 `continue`，程序会一直跑（如果没有断点）。

**需要观察的现象**：终端 A 启动后会**阻塞等待**（因为 `accept` 在等连接）；终端 B 一旦执行 `gdb-remote 8000`，连接建立，随后 LLDB 会自动发出一连串 `qHostInfo`、`qRegisterInfo`、`qfThreadInfo` 握手查询。

**预期结果**：LLDB 能成功 attach，提示符显示进程已连接；`continue` 后程序恢复执行，再按 Ctrl-C 可中断。**运行结果待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 gdb 模式要把 `stop_on_fault` 设为 true，而 normal 模式为 false？
**答案**：调试时希望程序在异常发生处停下供人检查；正常运行时则希望异常走正常的 trap 派发路径或直接报告崩溃退出，而不是卡住。

**练习 2**：`run_until_interrupt` 是怎么知道“宿主想暂停”的？
**答案**：它在每批指令之间用 `can_read_file_descriptor(client_socket)` 探测 socket 是否可读；LLDB 的中断会在 socket 上产生数据，于是循环跳出。这是一个轮询而非信号中断的实现。

---

### 4.3 断点：指令替换与命中

#### 4.3.1 概念说明

调试器的断点要解决一个看似矛盾的需求：**让程序在某个地址暂停，但又不破坏那里的代码**。在硬件上，可以用 JTAG 注入（见 u11-l1）；在模拟器里，最简单优雅的办法是**软件断点——把目标地址的那条指令临时换成一条“特殊指令”**，当解释器执行到这条特殊指令时就知道“这里被下了断点”，于是停下来。

Nyuzi 模拟器选用的特殊指令是 `0x707fffff`，记作 `BREAKPOINT_INST`。它之所以能当断点标记，是因为它**使用了保留的指令格式**，正常程序里不会出现；解释器在执行每条指令前本来就要按格式分派，碰到这条“非法”格式时顺手查一下断点表即可。这样做的好处是：**不必为每条指令都查一次断点表**——只有真正碰到 `0x707fffff` 才查，是个零开销优化。

断点还需要解决“继续执行”的问题：命中断点后，内存里那个地址仍然放着 `0x707fffff`。如果直接 continue，又会立刻再次命中、陷入死循环。所以需要一个 `restart` 标志，表示“下一次再走到这个地址时，请把原始指令换回来执行一次，然后再重新武装断点”。

#### 4.3.2 核心流程

设断点 `dbg_set_breakpoint(pc)`：

```
1. 查表确认 pc 处尚未设断点（否则报错）
2. 校验 pc < memory_size 且 4 字节对齐
3. 新建断点节点 { address=pc, original_instruction=memory[pc/4], restart=false }
4. 把 memory[pc/4] 改写成 BREAKPOINT_INST（0x707fffff）
```

执行时遇到 `BREAKPOINT_INST`（在 `execute_instruction` 内）：

```
查 lookup_breakpoint(pc-4):
 ├─ 没找到 → 这条 0x707fffff 是程序自带的非法指令 → 抛 TT_ILLEGAL_INSTRUCTION
 └─ 找到 bp:
     ├─ 若 bp->restart 或 proc->single_stepping:
     │     // “跨过断点”：用原指令执行一次
     │     bp->restart = false
     │     instruction = bp->original_instruction
     │     goto restart            // 用真指令重新走一遍分派
     └─ 否则（首次命中）:
           bp->restart = true       // 标记下次要跨过
           thread->pc -= 4          // PC 回退到这条指令
           return false             // 通知上层“停下了”
```

继续（continue）时，`execute_instructions` 的批处理循环发现 `execute_instruction` 返回 false（命中断点），就整体返回 false，于是 `run_until_interrupt` 跳出循环，桩回送 `S05`。下次再 continue，PC 重新指向这条已被 `restart=true` 标记的断点，于是走“跨过”分支，执行真正的指令后继续。

#### 4.3.3 源码精读

特殊指令常量及其设计意图（注释解释了“用保留格式做触发、避免每条指令查表”的优化）：[tools/emulator/processor.c:47-52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L47-L52)。

断点节点结构（链表，含原指令与 restart 标志）：[tools/emulator/processor.c:138-144](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L138-L144)。

设断点：保存原指令、改写为 `BREAKPOINT_INST`。注意第 531–532 行特判——若原指令本身已是 `0x707fffff`，就把它当成 NOP 保存，避免“原指令就是断点标记”导致无限循环——[tools/emulator/processor.c:511-536](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L511-L536)。

清断点：把原指令写回内存、从链表摘除节点：[tools/emulator/processor.c:538-556](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L538-L556)。

命中的核心逻辑在解释器主分派里。当指令最高位为 0（落在立即数算术分支）且等于 `BREAKPOINT_INST` 时进入断点处理：[tools/emulator/processor.c:2046-2074](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L2046-L2074)。

- 第 2048 行用 `thread->pc - 4` 查断点——因为在模拟器约定里，取指后 PC 已前进到下一条，所以当前指令地址是 `pc-4`（u8-l1 讲过 `-v` 跟踪同样用 `pc-4`）。
- 第 2061–2067 行是“跨过”分支：`restart` 或 `single_stepping` 为真时，换回原指令、`goto restart` 真正执行一次。
- 第 2068–2073 行是“首次命中”：置 `restart=true`、PC 回退、`return false` 停机。

这个 `return false` 一路向上传播：`execute_instruction` 返回假 → `execute_instructions` 的循环里 [tools/emulator/processor.c:422-423](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L422-L423)（随机调度）与 [tools/emulator/processor.c:445-446](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L445-L446)（轮询调度）整体返回假 → `run_until_interrupt` 跳出。

桩侧把 `Z0/z0` 命令接到这两个函数：[tools/emulator/remote-gdb.c:517-532](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L517-L532)。

#### 4.3.4 代码实践

**目标**：在 LLDB 里设断点，并亲眼看到内存里被替换的 `0x707fffff`。

**步骤**：

1. 按 4.2.4 启动 gdb 模式并用 LLDB attach。
2. 在 LLDB 里：`(lldb) break set --name main`（或 `b main`），然后 `(lldb) continue`，程序会停在 main。
3. 在 LLDB 里查看 main 的地址：`(lldb) image lookup -n main`，得到地址 `A`。
4. 读这个地址的字：`(lldb) memory read --size 4 --format x A`。

**需要观察的现象**：第 4 步读出的 4 字节应是 `ff ff 7f 70`（小端序的 `0x707fffff`），而不是 main 的真实第一条指令。

**预期结果**：确认断点 = 内存里那条指令被临时替换成了 `0x707fffff`。再 `(lldb) breakpoint delete`，重复 `memory read`，应看到原指令被恢复（对应 `dbg_clear_breakpoint` 把 `original_instruction` 写回）。**运行结果待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：如果程序自己的代码里恰好有一条 `0x707fffff`，且该地址没设断点，会发生什么？
**答案**：`lookup_breakpoint` 返回 NULL，解释器把它当作真正的非法指令，抛出 `TT_ILLEGAL_INSTRUCTION` 陷阱（见命申逻辑的“没找到”分支）。这正是把保留格式当断点标记的代价——程序不应主动使用该编码。

**练习 2**：`restart` 标志解决了什么问题？没有它会怎样？
**答案**：它解决“命中断点后继续执行会再次立即命中”的死循环。continue 时先让原指令执行一次（`restart=true` 触发替换分支），执行后清零、重新武装断点。没有它，程序会在断点处无限暂停。

---

### 4.4 单步、寄存器与内存访问

#### 4.4.1 概念说明

调试器还要能：**单步**（一次只执行一条指令）、**读/写寄存器**、**读/写内存**。这些在模拟器里都极其直接——因为架构状态就摆在 C 结构体和内存数组里，所谓的“读写”就是数组下标访问。

需要注意 Nyuzi 的寄存器模型（u2-l1）：32 个标量寄存器 s0–s31、32 个向量寄存器 v0–v31（每个 16 通道 × 32 位 = 512 位）、加一个 PC。桩把它们统一编号成一个“GDB 寄存器空间”：编号 0–31 是标量、32–63 是向量、64 是 PC。

单步的实现借助 4.3 提到的 `single_stepping` 全局标志：单步时把它置真，这样即使要步过的那条指令地址上正好有断点（内存里是 `0x707fffff`），也会走“换回原指令执行一次”的分支，**保证单步真的执行了一条程序指令，而不是停在断点上**。

内存访问有一个**重要限制**：模拟器的 `dbg_read_memory_byte` / `dbg_write_memory_byte` **不做地址翻译**。它们直接按物理（平坦）地址读写内存数组。当被调试程序启用了 MMU/虚拟内存时，调试器手里拿到的是虚拟地址，桩却当物理地址用，结果就完全错乱——这就是 README 里“调试器在虚拟内存启用时不可用”的根因。

#### 4.4.2 核心流程

单步 `dbg_single_step(thread)`：

```
proc->single_stepping = true
execute_instruction(该线程)     // 只跑一条
timer_tick()                     // 维持定时器/中断时序
（single_stepping 会在下次批量 execute_instructions 开始时被清零）
```

读寄存器（`p` 命令，reg_id 来自包）：

```
若 reg_id < 32   → dbg_get_scalar_reg → endian_swap → "%08x"   // 标量
若 reg_id < 64   → dbg_get_vector_reg → 16 个通道各 "%08x"     // 向量，128 位 hex
若 reg_id == 64  → dbg_get_pc → endian_swap → "%08x"           // PC
```

读内存（`m addr,len`）：从 addr 起逐字节 `dbg_read_memory_byte` 转 `%02x`。
写内存（`M addr,len:hex`）：`decode_hex_byte` 还原后逐字节 `dbg_write_memory_byte`。

> 关于字节序：Nyuzi 是小端。`%08x` 默认先打印高位字节，与“目标小端序（低位字节在前）”相反，故先 `endian_swap32` 再格式化，得到的 hex 串恰好是低位字节在前，符合协议“按目标字节序传输”的约定（`qHostInfo` 也声明 `endian:little`）。内存读写则按地址自然顺序逐字节，无需交换。

#### 4.4.3 源码精读

单步：[tools/emulator/processor.c:460-465](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L460-L465)。它把 `single_stepping` 置真，然后只对**指定线程**执行一条指令（注意：单步只动这一个线程，其他线程不动，这点在桩的 `vCont` 处理里有体现）。

读 PC：[tools/emulator/processor.c:455-458](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L455-L458)，就是返回线程结构体里的 `pc` 字段。

标量寄存器读写（直接读写 `scalar_reg[]` 数组）：[tools/emulator/processor.c:467-477](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L467-L477)。
向量寄存器读写（`memcpy` 整个 16 通道数组）：[tools/emulator/processor.c:479-491](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L479-L491)。

内存读写——注意第 493–496 行那段注释，明确说明**不做地址翻译、无法处理 TLB 缺失**，并给出两种可能的改进方向：[tools/emulator/processor.c:493-509](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L493-L509)。

桩侧把 `p/g` 命令接到寄存器接口（注意 reg_id 分段：<32 标量、<64 向量、==64 PC）：[tools/emulator/remote-gdb.c:338-374](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L338-L374)。写寄存器 `G`：[tools/emulator/remote-gdb.c:377-406](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L377-L406)（注意 PC 写入未实现，返回空应答）。

桩侧把 `m/M` 命令接到内存接口：[tools/emulator/remote-gdb.c:298-335](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/remote-gdb.c#L298-L335)。

字节序工具 `endian_swap32`：[tools/emulator/util.h:35-41](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/util.h#L35-L41)。向量 hex 解析 `parse_hex_vector` 声明在 [tools/emulator/util.h:111-112](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/util.h#L111-L112)。

已知限制，官方明确写了三条（**cosim 模式不支持调试器**、**开启优化后变量可能读不到**、**虚拟内存启用时调试器不工作**）：[tools/emulator/README.md:81-85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/README.md#L81-L85)。第三条的根因正是上面那段“内存访问不翻译”的注释。

#### 4.4.4 代码实践

**目标**：单步几条指令，读写寄存器与内存，并与 `-v` 跟踪对账。

**步骤**：

1. gdb 模式 + LLDB attach，在 main 设断点并 `continue` 停下。
2. `(lldb) register read` 读全部标量寄存器与 pc，记下 `pc`、`s0`–`s5`。
3. 连续 `(lldb) stepi` 三次（单步三条机器指令），再 `register read`。
4. 读一段内存：`(lldb) memory read --size 4 --format x $sp`，查看栈顶几个字。
5. 对照验证：用 `bin/nyuzi_emulator -v <program>.hex`（非 gdb 模式）跑到同一 PC，比较 `-v` 打印的寄存器写回值与你在调试器里看到的值。

**需要观察的现象**：每次 `stepi` 后 PC 增加 4（遇到分支除外），被写回的寄存器值与 `-v` 跟踪里 `[st N] sX <= value` 的行一致。

**预期结果**：单步与寄存器读写在**未启用虚拟内存**时完全可靠；`memory read` 直接命中平坦内存。**记录一条关键结论**：尝试对一个启用 MMU 的程序（例如跑 `software/kernel`）做 `memory read <用户态虚拟地址>`，会读到错误的物理内存内容——这就是 [tools/emulator/processor.c:493-496](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L493-L496) 注释所指的限制。**运行结果待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：为什么 `dbg_single_step` 要先把 `single_stepping` 置真？
**答案**：为了让“跨过断点”分支生效。如果单步恰好落在一条已被替换为 `0x707fffff` 的指令上，`single_stepping` 会强制换回原指令执行一次，否则单步会被断点逻辑误判为“命中”而原地不动。

**练习 2**：GDB 寄存器编号 0–31、32–63、64 分别映射到 Nyuzi 的什么？
**答案**：0–31 → 标量寄存器 s0–s31；32–63 → 向量寄存器 v0–v31（每个 512 位，传 128 位 hex）；64 → PC。

**练习 3**：为什么在虚拟内存启用时调试器不可用？
**答案**：因为 `dbg_read_memory_byte`/`dbg_write_memory_byte` 直接用传入地址索引平坦内存数组，不做 TLB 翻译；调试器给出的是虚拟地址，桩却当物理地址用，地址对不上，数据全错。且调试器触发的访问无法像正常执行那样处理 TLB 缺失 trap。

---

## 5. 综合实践

把本讲四个模块串起来，完整走一次源码级调试，并亲自验证“虚拟内存限制”。

**背景程序**：用 `software/apps/hello_world`（或任一带 `-g` 编译的 app）。先用 `llvm-objdump -d -S hello_world.elf > hello_world.lst`（见 [tools/emulator/README.md:103-106](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/README.md#L103-L106)）生成带源码的汇编清单。

**任务**：

1. **启动调试链路**：`bin/nyuzi_emulator -m gdb hello_world.hex`，另一终端 LLDB `gdb-remote 8000` attach（4.2）。
2. **设断点并观察指令替换**：`b main`、`continue`；用 `memory read` 确认断点处被替换成 `0x707fffff`（4.3）。
3. **单步 + 寄存器对账**：`stepi` 几条，`register read s0 s1 sp pc`，与 `hello_world.lst` 当前 PC 处的指令、以及 `-v` 跟踪对账，解释每一步寄存器为何这样变化（4.4）。
4. **读写内存**：用 `memory read` 读 `.rodata` 里的 “Hello world” 字符串地址（对照 lst），确认能读到正确字节。
5. **追踪一条命令的全旅程**：挑一条 `p <某寄存器>` 命令，从 LLDB 发出 → `read_packet`（4.1）→ `switch('p')`（4.2）→ `dbg_get_scalar_reg`（4.4）→ `endian_swap32` → `send_response_packet`，在源码里标出这五站对应的行号，写成一份调用链笔记。
6. **验证限制**：换一个启用虚拟内存的程序（内核或开启了 MMU 的 app），重复 `memory read <虚拟地址>`，记录现象，并对照 [tools/emulator/processor.c:493-509](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L493-L509) 与 [tools/emulator/README.md:85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/README.md#L85) 写下根因。

**交付物**：一份调试日志 + 一条带行号的命令调用链 + 一段对“虚拟内存下内存读失效”的现象与根因说明。**完整运行结果待本地验证。**

## 6. 本讲小结

- 模拟器用 **GDB 远程串行协议**（`$正文#校验和` + `+` 确认）在 8000 端口扮演调试桩，LLDB 通过 `gdb-remote 8000` 附着，桩按 `request[0]` 分派命令。
- **gdb 模式** 与 normal 模式的关键差异是：打开 `stop_on_fault`、把主循环变成“命令服务台”，`continue` 用 `run_until_interrupt` 跑到断点或宿主中断。
- **断点** 用软件指令替换实现：把目标地址指令临时换成保留编码 `0x707fffff`（`BREAKPOINT_INST`），解释器只在遇到该编码时查断点表，是零开销优化；`restart` 标志解决“继续执行不再重复命中”。
- **单步** 借 `single_stepping` 标志确保跨过断点时执行真正的指令；寄存器按 0–31 标量 / 32–63 向量 / 64 PC 统一编号，值经 `endian_swap32` 按小端序传输。
- **硬限制**：内存读写不做地址翻译，因此**虚拟内存启用时调试器不可用**；此外 cosim 模式不支持调试器、开优化后变量可能读不到。
- 这套机制与 u11-l1 的硬件侧 JTAG 调试互补：一个面向模拟器（功能级、快、无 JTAG 时序），一个面向真实硬件/RTL。

## 7. 下一步学习建议

- 想把“内存访问不翻译”这个限制补上，可以阅读 [tools/emulator/processor.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c) 里的 `translate_address` 与 TLB 相关逻辑（u7-l1），尝试给 `dbg_read_memory_byte` 增加一次“尽力翻译、缺失则报错”的路径。
- 对比硬件侧调试，回到 **u11-l1 片上调试器与 JTAG**，理解两者在能力与限制上的对称与不对称（如都无单步硬件支持、都受多周期指令限制）。
- 配合 **u11-l2 性能计数器与 profiling**，把“功能调试（本讲）”与“性能剖析”组合成完整的调优工作流。
- 若对协议细节感兴趣，可对照 LLDB/GDB 官方远程协议文档，审视 `remote-gdb.c` 里那些 `XXX hack` 注释（如 `vCont` 的单线程步进近似、`H` 命令忽略操作类型），思考如何让桩更标准、更兼容 GDB。
