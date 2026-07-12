# 监视点机制

## 1. 本讲目标

本讲讲解 NEMU 简单调试器 SDB 的「监视点（watchpoint）」机制。学完后你应该能够：

- 说出监视点要解决的问题，以及它与单步执行（`si`）、继续执行（`c`）的区别。
- 理解 NEMU 用「静态内存池 + 空闲链表」管理对象的设计动机与工作方式。
- 读懂 `init_wp_pool()` 如何把一整块静态数组「编织」成一条空闲链表。
- 设计并实现 `new_wp()` / `free_wp()` 这一对申请/释放操作。
- 把监视点与上一讲（u2-l7）的表达式求值 `expr()` 结合，实现「每步检查表达式值是否变化，变化则暂停 CPU」。
- 在 SDB 命令表里新增 `w EXPR`（设置监视点）与 `d N`（删除监视点）命令。

本讲是 PA1 阶段 SDB 的收尾：词法分析（u2-l6）→ 表达式求值（u2-l7）→ 监视点（本讲）。监视点把前面两个模块「消费」起来，让表达式真正参与到 CPU 的执行控制中。

## 2. 前置知识

### 2.1 什么是监视点

在 GDB 这类调试器里，断点（breakpoint）是「执行到某条指令时停下来」，而**监视点（watchpoint）是「某个表达式（通常是一个变量或内存地址）的值发生变化时停下来」**。例如你想知道「到底是哪一行代码把变量 `x` 改成了 0」，断点帮不上忙（你不知道是哪一行），但监视点可以：你设置 `watch x`，只要 `x` 的值一变，程序立刻暂停，你就能看到是哪条指令改的。

NEMU 的 SDB 要实现的就是这种语义。由于 NEMU 并不懂得客机程序里的「变量名」，它只能用**表达式**来描述被监视的值——这正是上一讲 `expr()` 的用武之地。

### 2.2 单链表与头插法

本讲的实现大量用到单链表。你需要熟悉：

- 一个节点除了数据，还有一个 `next` 指针指向下一个节点。
- **头插法**：把一个节点插入链表头部，只需 `node->next = head; head = node;` 两步，O(1)。
- **头删法**：从头部取走一个节点，只需 `head = head->next;`，O(1)。

我们会用头插法/头删法在两条链表之间搬运节点，从而实现监视点的申请与释放。

### 2.3 静态数组 vs 动态分配

C 语言里分配一组对象有两条路：`malloc` 在堆上按需分配、用完 `free`；或者**直接开一个固定大小的数组**，自己管理哪些元素「在使用」、哪些「空闲」。后者就是「对象池」。本讲会解释 NEMU 为什么选择对象池。

### 2.4 承接前两讲

- u2-l5 讲了 SDB 的命令表 `cmd_table`：新增命令只需加一个 `{name, description, handler}` 三元组。本讲的 `w` / `d` 命令就挂在这张表上。
- u2-l7 讲了 `word_t expr(char *e, bool *success)`：把字符串表达式求值为一个 `word_t`，失败时把 `*success` 置 `false`。本讲的监视点每步都要调用它。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [src/monitor/sdb/watchpoint.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/watchpoint.c) | 监视点的核心实现 | 静态池 `wp_pool`、两条链表 `head`/`free_`、`init_wp_pool()`，以及待实现的 `new_wp`/`free_wp`/检查 |
| [src/monitor/sdb/sdb.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.h) | SDB 对外接口 | 声明了 `expr()`，监视点检查依赖它 |
| [src/monitor/sdb/sdb.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c) | 命令框架与初始化 | `init_sdb()` 调用 `init_wp_pool()`；`w`/`d` 命令加在此处的 `cmd_table` |
| [src/cpu/cpu-exec.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c) | CPU 执行主循环 | `execute()` 循环是「每步检查监视点」的插入点 |
| [include/utils.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/utils.h) | 全局状态定义 | `NEMUState` 枚举：值变化时把状态置为 `NEMU_STOP` 让 CPU 暂停 |

> ⚠️ 重要：`watchpoint.c` 在当前仓库里是一个**骨架**——`WP` 结构只有 `NO` 和 `next`，`new_wp`/`free_wp`/检查函数都还是 `/* TODO */`，没有实现。本讲要做的正是把这些 TODO 补上。因此本讲出现的实现代码除非明确标注「项目原有代码」，否则都是**示例代码**，需要你亲手写进源文件。

## 4. 核心概念与源码讲解

### 4.1 静态内存池：wp_pool 与 NR_WP

#### 4.1.1 概念说明

我们需要一种方式来「持有」若干个监视点对象。最直观的做法是每次新建监视点时 `malloc(sizeof(WP))`、删除时 `free`。但 NEMU 选择了**对象池（object pool）**：在编译期就开好一块固定大小的数组，所有监视点对象都从这块数组里取。

为什么这么做？有三个理由：

1. **教学清晰**：malloc/free 会引入「忘记释放→内存泄漏」「重复释放→未定义行为」等额外的心智负担，与「理解监视点本身」无关。对象池把内存管理简化成「在两条链表之间搬运节点」。
2. **可预测**：监视点数量有明确上限，不会因为用户设置过多监视点而无限吃内存。
3. **零分配开销**：申请/释放只是改几个指针，不进入 libc 的堆分配器，对一个「每条指令都要检查一次」的热路径很友好。

代价是：监视点总数不能超过池的大小。

#### 4.1.2 核心流程

- 在编译期定义上限 `NR_WP`。
- 声明一个长度为 `NR_WP` 的静态数组 `wp_pool`，每个元素是一个 `WP` 对象。
- 每个 `WP` 有一个编号 `NO`（0 到 NR_WP−1），方便用户用编号引用。
- 「哪些 `WP` 正被使用、哪些空闲」由下一节的链表来区分，数组本身只是「存储仓库」。

#### 4.1.3 源码精读

`NR_WP` 定义了池的容量，`WP` 是监视点对象类型，`wp_pool` 是那块静态数组：

```c
#define NR_WP 32

typedef struct watchpoint {
  int NO;
  struct watchpoint *next;

  /* TODO: Add more members if necessary */

} WP;

static WP wp_pool[NR_WP] = {};
```

这是项目原有代码，见 [src/monitor/sdb/watchpoint.c:18-29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/watchpoint.c#L18-L29)。

要点：

- `NR_WP` 设为 `32`，即同时最多 32 个监视点（这是一个教学上够用的数字）。
- `wp_pool[NR_WP] = {}` 末尾的 `= {}` 把整个数组**零初始化**，所有字节清 0，因此初始时所有 `next` 都是 `NULL`。
- `WP` 目前只有 `NO`（编号）和 `next`（链表指针）。那个 `/* TODO: Add more members if necessary */` 正是留给你的：监视点要记录「监视的表达式」与「上次的值」，需要在这里加字段。
- 三者都是 `static`，意味着它们只在 `watchpoint.c` 这个翻译单元内可见，外部文件要通过函数（如 `new_wp`）来访问，而非直接碰数组。

#### 4.1.4 代码实践

**实践目标**：直观感受「池」的容量约束。

**操作步骤**：

1. 打开 [src/monitor/sdb/watchpoint.c:18](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/watchpoint.c#L18)，把 `NR_WP` 从 `32` 临时改成 `2`。
2. 完成 4.3 的 `new_wp` 后，在 SDB 里连续设置 3 个监视点。

**需要观察的现象**：第 3 次 `new_wp` 时，`free_` 链表已空，你的 `new_wp` 应当能优雅地报告「没有空闲监视点」（例如 `assert(0)` 或打印错误），而不是越界访问。

**预期结果**：理解了容量上限的含义后，把 `NR_WP` 改回 `32`。

**待本地验证**：具体输出取决于你 `new_wp` 里对「池空」的处理方式。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `wp_pool` 用 `static` 修饰？如果去掉 `static` 会怎样？

**答案**：`static` 限制该全局变量仅在本 `.c` 文件可见（内部链接）。这样做把池的实现细节封装在 `watchpoint.c` 内，其他文件（如 `sdb.c`）不能直接读写 `wp_pool`，只能通过 `new_wp`/`free_wp` 等函数操作，保证了链表始终被正确维护。去掉 `static` 后它变成外部链接，任何文件都能直接改数组，可能绕过链表维护逻辑导致状态不一致。

**练习 2**：`wp_pool[NR_WP] = {}` 末尾的 `= {}` 能否省略？

**答案**：因为 `wp_pool` 是 `static` 全局变量，C 标准保证它即使不显式初始化也会被零初始化，所以理论上可以省略。写 `= {}` 是显式表达意图，让读者一眼看出「初始化为全零」。

### 4.2 双链表管理：head / free_ 与 init_wp_pool

#### 4.2.1 概念说明

光有一块数组还不够，我们还需要随时知道「现在哪些 `WP` 在被用户使用、哪些是空闲可分配的」。NEMU 的做法是用**两条单向链表**来管理：

- `head`：**正在使用**的监视点链表。用户每设置一个监视点，对应节点就出现在这里。
- `free_`：**空闲**的监视点链表。可以分配给新请求的节点都在这里。

> 小贴士：变量名叫 `free_` 而不是 `free`，是因为 `free` 是 C 标准库函数名（`malloc`/`free`），用它作变量名会遮蔽库函数、引起混乱。加一个下划线是最简单的规避。

申请一个监视点 = 把一个节点从 `free_` 搬到 `head`；释放一个监视点 = 把一个节点从 `head` 搬回 `free_`。整个生命周期里，32 个节点始终在两条链表之间流转，一个都不会「丢」。

#### 4.2.2 核心流程

初始化时，没有任何监视点在使用，所以：

- `head = NULL`（空链表）。
- `free_` 指向第 0 个节点，第 0 个节点指向第 1 个，……，第 31 个节点指向 `NULL`。即把 `wp_pool` 的 32 个节点**首尾相接串成一条链**。

用文字画出来就是：

```
free_ -> [0] -> [1] -> [2] -> ... -> [31] -> NULL
head   = NULL
```

#### 4.2.3 源码精读

`init_wp_pool()` 负责把池「编织」成空闲链：

```c
static WP *head = NULL, *free_ = NULL;

void init_wp_pool() {
  int i;
  for (i = 0; i < NR_WP; i ++) {
    wp_pool[i].NO = i;
    wp_pool[i].next = (i == NR_WP - 1 ? NULL : &wp_pool[i + 1]);
  }

  head = NULL;
  free_ = wp_pool;
}
```

这是项目原有代码，见 [src/monitor/sdb/watchpoint.c:29-40](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/watchpoint.c#L29-L40)。

逐行理解：

- 循环给每个节点编号：`wp_pool[i].NO = i`，这样用户可以用编号 0~31 引用监视点。
- 关键一行 `wp_pool[i].next = (i == NR_WP - 1 ? NULL : &wp_pool[i + 1])`：除最后一个节点（`i == 31`）的 `next` 置 `NULL` 外，其余都指向「数组中的下一个元素」。注意 `next` 指向的是 `&wp_pool[i+1]`（数组元素的地址），而不是 `wp_pool[i+1].next`——前者是「下一个节点本身」，后者是「下一个节点的 next 字段」，二者完全不同。
- 循环结束后，`free_ = wp_pool`（即 `&wp_pool[0]`）让空闲链表头部指向第 0 个节点；`head = NULL` 表示没有使用中的监视点。

`init_wp_pool()` 何时被调用？在 `init_sdb()` 里，紧跟着词法分析的正则预编译：

```c
void init_sdb() {
  /* Compile the regular expressions. */
  init_regex();

  /* Initialize the watchpoint pool. */
  init_wp_pool();
}
```

见 [src/monitor/sdb/sdb.c:137-143](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L137-L143)。回顾 u1-l3，`init_sdb()` 又由 `init_monitor()` 调用，所以在 CPU 开始执行任何指令之前，监视点池一定已经准备好了。

#### 4.2.4 代码实践

**实践目标**：用纸笔（或注释）画出初始化后的链表结构，确认理解。

**操作步骤**：

1. 假设把 `NR_WP` 临时改成 `4`，手算 `init_wp_pool()` 执行后 `wp_pool[0..3]` 各自的 `NO` 与 `next`。
2. 标出 `head` 与 `free_` 的指向。

**预期结果**（手算答案）：

```
wp_pool[0].NO = 0, next = &wp_pool[1]
wp_pool[1].NO = 1, next = &wp_pool[2]
wp_pool[2].NO = 2, next = &wp_pool[3]
wp_pool[3].NO = 3, next = NULL
head  = NULL
free_ = &wp_pool[0]
```

如果你画出的图与上面一致，就说明你已经掌握了两条链表的初始化。

#### 4.2.5 小练习与答案

**练习 1**：把 `init_wp_pool` 循环里的 `wp_pool[i].next = (i == NR_WP - 1 ? NULL : &wp_pool[i + 1])` 改写成不使用三目运算符的等价形式。

**答案**：

```c
if (i == NR_WP - 1) {
  wp_pool[i].next = NULL;
} else {
  wp_pool[i].next = &wp_pool[i + 1];
}
```

**练习 2**：如果初始化时忘记写 `head = NULL;`，会出什么问题？

**答案**：`head` 是 `static` 全局变量，会被零初始化为 `NULL`，所以「恰好」没问题。但如果哪天把 `head` 改成非 static、或这段代码被复用到 `head` 已被使用过的场景，就会读到旧指针，把无关内存当成监视点。显式写 `head = NULL;` 是一种防御性、自解释的写法，值得保留。

### 4.3 监视点的申请与释放：new_wp / free_wp

#### 4.3.1 概念说明

池和链表准备好后，我们需要两个基本操作：

- `WP *new_wp()`：从 `free_` 链表头部取出一个空闲节点，**移入** `head` 链表头部，返回该节点指针供调用方填写表达式等信息。若 `free_` 为空（池耗尽），应当报错。
- `void free_wp(WP *wp)`：把 `wp` 从 `head` 链表中摘下，**归还**到 `free_` 链表头部。

注意一个设计约定：**这两个函数都只搬运节点，不维护数据**。也就是说，`new_wp` 不负责填写表达式（由 `w` 命令在拿到节点后填写），`free_wp` 不负责清空表达式。这样职责单一，链表逻辑和数据逻辑解耦。

为什么都从头部操作？因为单链表的头插/头删是 O(1)；如果从尾部或中间操作，就需要遍历，复杂度 O(n)。监视点的顺序对正确性没有影响（我们每步都会遍历全部检查），所以用最简单的头部操作即可。

#### 4.3.2 核心流程

`new_wp()` 伪代码：

```
function new_wp():
  if free_ == NULL:        // 池空
    报错并终止（assert 或 return NULL）
  p = free_                // 取出空闲链表头
  free_ = p->next          // 空闲链表头后移
  p->next = head           // 把 p 头插到使用链表
  head = p
  return p
```

`free_wp(wp)` 伪代码（接受一个编号或指针，从 `head` 中找到并摘除）：

```
function free_wp(target):
  在 head 链表中找到目标节点 prev/p
  若找不到：报错
  从 head 链表摘除 p（prev->next = p->next，或 head = p->next 若 p 是头）
  p->next = free_          // 头插到空闲链表
  free_ = p
```

搬运前后，`head` 与 `free_` 的节点总数之和恒为 `NR_WP`，没有任何节点「泄漏」。

#### 4.3.3 源码精读

当前仓库里这两个函数**尚未实现**，只有一行 TODO 注释占位：

```c
/* TODO: Implement the functionality of watchpoint */
```

见 [src/monitor/sdb/watchpoint.c:42-43](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/watchpoint.c#L42-L43)。下面给出**示例代码**供你参考（需要你自己写入并验证）。

> 示例代码：`new_wp`（头删 free_、头插 head）

```c
/* 示例代码：并非项目原有实现 */
WP *new_wp() {
  if (free_ == NULL) {
    printf("No free watchpoint available (pool exhausted, NR_WP=%d)\n", NR_WP);
    assert(0);
    return NULL;
  }
  WP *p = free_;
  free_ = p->next;     // 从空闲链表头部摘下
  p->next = head;      // 头插到使用链表
  head = p;
  return p;
}
```

> 示例代码：`free_wp`（按编号从 head 摘除、头插 free_）

```c
/* 示例代码：并非项目原有实现 */
void free_wp(int no) {
  WP *p, *prev = NULL;
  for (p = head; p != NULL; prev = p, p = p->next) {
    if (p->NO == no) break;
  }
  if (p == NULL) {
    printf("Watchpoint #%d not found\n", no);
    return;
  }
  // 从 head 链表摘除 p
  if (prev == NULL) head = p->next;
  else prev->next = p->next;
  // 头插到 free_
  p->next = free_;
  free_ = p;
}
```

这两个函数都依赖 `head`/`free_` 的链表不变量：`head` 始终串联所有「使用中」节点，`free_` 始终串联所有「空闲」节点。只要你只通过这两个函数（以及 `init_wp_pool`）改链表，不变量就不会被破坏。

> 小贴士：因为 `head`/`free_`/`wp_pool` 都是 `static`，`new_wp`/`free_wp` 必须写在 `watchpoint.c` 内部，否则访问不到。如果你想在 `sdb.c` 里调用它们，需要在某个头文件（如 `sdb.h`）里加上函数声明。

#### 4.3.4 代码实践

**实践目标**：实现并验证 `new_wp` / `free_wp` 的链表搬运逻辑。

**操作步骤**：

1. 先在 `WP` 结构里加上记录表达式字符串的字段（见综合实践），暂时不影响本步。
2. 把上面两段示例代码写入 [src/monitor/sdb/watchpoint.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/watchpoint.c)（记得 `#include <stdio.h>` 与 `#include <assert.h>`，或复用项目已包含的头）。
3. 在 `sdb.h` 里声明 `WP *new_wp();` 与 `void free_wp(int no);`，让 `sdb.c` 能调用。
4. 临时写一个测试：在 `init_wp_pool()` 末尾连续 `new_wp()` 三次，打印每次返回节点的 `NO`；再 `free_wp(1)`，再 `new_wp()` 一次，看返回的 `NO` 是不是 1（因为 1 刚被归还到 free_ 头部，会被优先取出）。

**需要观察的现象**：

- 前三次 `new_wp()` 返回的 `NO` 应为 `0, 1, 2`（因为初始 free_ 从 0 开始）。
- `free_wp(1)` 后，节点 1 回到 free_ 头部。
- 再次 `new_wp()` 返回的 `NO` 应为 `1`（头插的逆操作）。

**预期结果**：验证「头删 free_ → 头插 head」「头删 head → 头插 free_」的对称性，理解 LIFO（后进先出）特性。

**待本地验证**：测试通过后请删除临时测试代码。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `new_wp` 从 `free_` 取节点用「头删」（`free_ = p->next`），而不遍历到尾部取？

**答案**：单链表头删是 O(1)，尾删需要遍历到倒数第二个节点、是 O(n)。监视点的分配顺序对正确性无影响（每步全量检查），所以用最省的头删即可。

**练习 2**：如果 `free_wp` 里忘记把节点头插回 `free_`（即只从 head 摘除、不归还），会发生什么？

**答案**：该节点会「泄漏」——既不在 `head` 也不在 `free_`，永远无法再被分配。多次设置/删除后，池会过早耗尽。这就是为什么必须保证搬运操作成对：从一条链摘下，就要插回另一条链。

### 4.4 每步检查值变化：监视点检查与表达式

#### 4.4.1 概念说明

到目前为止我们只是管理了「监视点对象」，但还没实现监视点的核心语义——**值变化时暂停 CPU**。要做到这一点，每个监视点必须记住两样东西：

1. **要监视的表达式**（字符串），比如 `$a0`、`*0x80001000`、`$a0 + 4`。
2. **上次求得的值**（`word_t`），用于和当前值比较。

每执行一条客机指令后，遍历 `head` 链表里每个监视点，用 `expr(wp->expr)` 重新求值：

- 若 `expr` 返回的值与 `wp` 记录的旧值不同 → 说明这个被监视的表达式变了，应当让 CPU 停下来。
- 否则继续执行。

如何「让 CPU 停下来」？回顾 u2-l5 与 u3-l9：SDB 通过 `cpu_exec(n)` 驱动 CPU，`cpu_exec` 内部的 `execute()` 循环每步都会检查 `nemu_state.state`，只要它不再是 `NEMU_RUNNING` 就会 `break` 退出循环。因此我们只需在检查到值变化时，把 `nemu_state.state` 置为 `NEMU_STOP`，`execute()` 自然会在当前这步结束后退出，控制权回到 SDB 主循环。

这就把「监视点」和「表达式求值」和「CPU 执行状态机」三者串了起来：

```
监视点表达式 ──expr()──> 当前值 ──比较──> 旧值
                                        │ 相等：继续
                                        │ 不等：nemu_state.state = NEMU_STOP
                                                        │
                                              execute() 循环检测到非 RUNNING，break
                                                        │
                                              cpu_exec 返回，SDB 回到提示符
```

#### 4.4.2 核心流程

完整的「每步检查」函数 `void test_watchpoints()`（名字自取）伪代码：

```
function test_watchpoints():
  for p in head 链表:
    bool ok
    new_val = expr(p->expr, &ok)      // 用 u2-l7 的求值器
    if not ok:
      报告表达式求值失败，跳过
      continue
    if new_val != p->old_val:
      打印：监视点 #p->NO 变化：旧值 -> 新值
      nemu_state.state = NEMU_STOP     // 让 execute() 停下
      // 不必 break：可继续报告其余变化，或直接 return
```

它要在哪里被调用？在 `execute()` 循环里，每执行完一条指令（`exec_once` 之后）调用一次。见 4.4.3。

`WP` 结构需要扩展两个字段。**示例代码**（修改 `WP` 定义）：

```c
/* 示例代码：扩展 WP 结构 */
typedef struct watchpoint {
  int NO;
  struct watchpoint *next;
  char expr[256];      // 监视的表达式字符串
  word_t old_val;      // 上次求得的值
} WP;
```

> 这里用定长数组 `char expr[256]` 而非 `char *` + malloc，是为了贯彻「零动态分配」的对象池风格。表达式长度超过 255 时截断即可。

#### 4.4.3 源码精读

先看「检查点」——`execute()` 循环当前的样子：

```c
static void execute(uint64_t n) {
  Decode s;
  for (;n > 0; n --) {
    exec_once(&s, cpu.pc);
    g_nr_guest_inst ++;
    trace_and_difftest(&s, cpu.pc);
    if (nemu_state.state != NEMU_RUNNING) break;
    IFDEF(CONFIG_DEVICE, device_update());
  }
}
```

见 [src/cpu/cpu-exec.c:74-83](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L74-L83)。`if (nemu_state.state != NEMU_RUNNING) break;`（[第 80 行](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L80)）就是我们要利用的退出点。只要某步之后 `nemu_state.state` 被改成 `NEMU_STOP`，下一次循环条件判断就会 `break`。

`nemu_state.state` 的取值在 [include/utils.h:23](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/utils.h#L23) 定义：

```c
enum { NEMU_RUNNING, NEMU_STOP, NEMU_END, NEMU_ABORT, NEMU_QUIT };
```

`NEMU_STOP` 表示「因调试（断点/监视点）而暂停，程序本身没结束」，这正是我们想要的状态——它和 `NEMU_END`/`NEMU_ABORT`（程序结束）不同。看 [src/cpu/cpu-exec.c:116-117](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L116-L117)：当 `execute()` 返回后，`NEMU_RUNNING` 会被改写为 `NEMU_STOP`，`cpu_exec` 接着返回，控制权交回 SDB 主循环，用户可以再次输入命令。我们提前把状态设成 `NEMU_STOP`，只是让这个「暂停」发生在值变化的当步，而不是跑到 `n` 条结束。

再看检查函数依赖的 `expr()` 声明，在 [src/monitor/sdb/sdb.h:21](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.h#L21)：

```c
word_t expr(char *e, bool *success);
```

它把字符串 `e` 求值为 `word_t`，失败时 `*success = false`。监视点检查每次都要调用它。

> 示例代码：检查函数与接入点

```c
/* 示例代码：在 watchpoint.c 实现 */
void test_watchpoints() {
  WP *p;
  for (p = head; p != NULL; p = p->next) {
    bool success = true;
    word_t new_val = expr(p->expr, &success);
    if (!success) {
      printf("Watchpoint #%d: expr '%s' evaluation failed\n", p->NO, p->expr);
      continue;
    }
    if (new_val != p->old_val) {
      printf("Hit watchpoint #%d at pc = " FMT_WORD
             ": %s changed from " FMT_WORD " to " FMT_WORD "\n",
             p->NO, cpu.pc, p->expr, p->old_val, new_val);
      p->old_val = new_val;       // 更新旧值为新值，便于下次比较
      nemu_state.state = NEMU_STOP;
    }
  }
}
```

接入 `execute()`（**示例代码**，修改 [src/cpu/cpu-exec.c:74-83](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L74-L83)）：

```c
/* 示例代码：在 exec_once 与 break 判断之间插入检查 */
static void execute(uint64_t n) {
  Decode s;
  for (;n > 0; n --) {
    exec_once(&s, cpu.pc);
    g_nr_guest_inst ++;
    trace_and_difftest(&s, cpu.pc);
    test_watchpoints();                              // 新增：每步检查监视点
    if (nemu_state.state != NEMU_RUNNING) break;
    IFDEF(CONFIG_DEVICE, device_update());
  }
}
```

注意插入位置在 `trace_and_difftest` 之后、`if (... != NEMU_RUNNING) break` 之前——这样一旦检查函数把状态改成 `NEMU_STOP`，紧接着的 `break` 判断就能捕获并退出。

> `test_watchpoints` 里用到 `cpu.pc`、`nemu_state`、`FMT_WORD`，这些来自项目头文件。`cpu_exec.c` 已 `#include <cpu/cpu.h>` 等，调用前确认 `test_watchpoints` 的声明可见（在 `cpu-exec.c` 里加 `void test_watchpoints();` 声明，或通过 `sdb.h` 暴露）。

#### 4.4.4 代码实践

**实践目标**：让一个监视点真正「在值变化时暂停 CPU」。

**操作步骤**：

1. 按 4.4.2 扩展 `WP`，加 `expr[256]` 与 `old_val`。
2. 实现 `test_watchpoints()`（4.4.3 示例）。
3. 在 `execute()` 中插入调用（4.4.3 示例）。
4. 完成一个临时的设置入口：在 SDB 里随便加一条命令（或临时在 `init_wp_pool` 后）调用 `new_wp()`，把表达式设为 `$a0`（或某个寄存器），并用 `expr()` 算出初始 `old_val`。

**需要观察的现象**：运行 `c`（继续执行）后，CPU 不再一路跑完，而是停在 `$a0` 第一次变化的那条指令处，并打印出变化前后的值与当时的 `pc`。

**预期结果**：屏幕出现形如 `Hit watchpoint #0 at pc = 0x...: $a0 changed from 0x0 to 0x...` 的提示，随后 `(nemu) ` 提示符回来，可用 `info r`（u5-l15 实现后）查看寄存器确认。

**待本地验证**：具体 `pc` 与寄存器值取决于加载的程序。若没有可运行的 RISC-V 程序，可先用 `p $pc + 1` 这类随 `pc` 自然变化的表达式做最小验证——每执行一条指令 `$pc` 必变，监视点应当几乎立刻命中。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `test_watchpoints` 在发现值变化后要执行 `p->old_val = new_val`？不更新会怎样？

**答案**：更新后，下次比较的基准就是新值，监视点语义是「每次相对上次的变化」。如果不更新，`old_val` 永远停在最初的值，那么暂停恢复后第一步就会因为「新值仍不等于最初的旧值」而**立刻再次命中**，陷入「每步都停」的死循环。

**练习 2**：把 `nemu_state.state = NEMU_STOP` 改成 `NEMU_END` 行不行？

**答案**：不行。`NEMU_END` 表示「程序正常结束」，`cpu_exec` 返回后会打印 `HIT GOOD/BAD TRAP` 并进入 `statistic`，之后再次输入命令会得到 `Program execution has ended. To restart the program, exit NEMU and run again.`（见 [src/cpu/cpu-exec.c:102-105](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L102-L105)），无法继续调试。`NEMU_STOP` 才是「可恢复的暂停」，是监视点与断点的正确状态。

**练习 3**：如果同时设置了 3 个监视点，其中 2 个在同一步同时变化，`test_watchpoints` 会怎样？

**答案**：按 4.4.3 的示例实现，循环会依次报告两个变化的监视点，并把状态设为 `NEMU_STOP`（设两次效果相同）。`execute()` 在本步结束后退出。用户因此能在一次暂停里看到所有同时变化的情况，符合直觉。

## 5. 综合实践

把四个最小模块串起来，完成 SDB 的 `w EXPR` 与 `d N` 命令，让监视点变成一个可交互使用的完整功能。

### 实践目标

在 SDB 中支持：

- `w EXPR`：新建一个监视点，监视表达式 `EXPR`，立即求值作为初始旧值。
- `d N`：删除编号为 `N` 的监视点。
- 程序运行期间，任一监视点表达式值变化时自动暂停，并打印变化信息。

### 操作步骤

1. **扩展 `WP`**：在 [src/monitor/sdb/watchpoint.c:20-26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/watchpoint.c#L20-L26) 的 `WP` 结构里加 `char expr[256];` 与 `word_t old_val;`。

2. **实现四个函数**（均放在 `watchpoint.c`，示例代码见 4.3.3 与 4.4.3）：
   - `WP *new_wp();`
   - `void free_wp(int no);`
   - `void test_watchpoints();`
   - 可选：`void list_watchpoints();` 用于打印当前监视点列表。

3. **暴露声明**：在 [src/monitor/sdb/sdb.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.h) 里声明上述函数，使 `sdb.c` 与 `cpu-exec.c` 可见。

4. **接入 `execute()`**：在 [src/cpu/cpu-exec.c:74-83](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L74-L83) 的循环里、`exec_once` 之后、`break` 判断之前，调用 `test_watchpoints();`。

5. **新增两条命令**：在 [src/monitor/sdb/sdb.c:57-68](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L57-L68) 的 `cmd_table` 里加两项，并实现 `cmd_w` / `cmd_d`（示例代码如下）。

   > 示例代码：`cmd_w`

   ```c
   /* 示例代码 */
   static int cmd_w(char *args) {
     if (args == NULL) { printf("Usage: w EXPR\n"); return 0; }
     bool success = true;
     word_t val = expr(args, &success);
     if (!success) { printf("Bad expression: %s\n", args); return 0; }
     WP *wp = new_wp();
     strncpy(wp->expr, args, sizeof(wp->expr) - 1);
     wp->expr[sizeof(wp->expr) - 1] = '\0';
     wp->old_val = val;
     printf("Set watchpoint #%d: %s = " FMT_WORD "\n", wp->NO, wp->expr, val);
     return 0;
   }
   ```

   > 示例代码：`cmd_d`

   ```c
   /* 示例代码 */
   static int cmd_d(char *args) {
     if (args == NULL) { printf("Usage: d N\n"); return 0; }
     int no = atoi(args);
     free_wp(no);
     printf("Deleted watchpoint #%d\n", no);
     return 0;
   }
   ```

   表项：
   ```c
   { "w", "Set a watchpoint on expression EXPR", cmd_w },
   { "d", "Delete watchpoint N", cmd_d },
   ```

6. **验证**（需要能跑的 RISC-V 程序，否则见下面的最小验证）：
   - `(nemu) w $pc + 1`
   - `(nemu) c`
   - 观察是否几乎立刻命中（因为每条指令后 `$pc` 都变）。
   - `(nemu) d 0` 后再 `c`，应一路跑完不再因该监视点暂停。

### 最小验证（无可用程序时）

由于 `$pc` 在每条指令后必然变化，`w $pc` 是最容易触发的监视点。设置后执行 `c`，应当立刻暂停并打印 `pc` 变化——这足以证明整条链路（`cmd_w` → `new_wp` → `test_watchpoints` → `nemu_state.state = NEMU_STOP` → `execute()` break）全部打通。

### 需要观察的现象与预期结果

- `w EXPR` 立即打印监视点编号与初始值。
- `c` 后，值变化时自动暂停，打印变化前后的值与当时 `pc`。
- 暂停后 `(nemu) ` 提示符恢复，可继续 `c` 或 `d N`。
- 删除监视点后，对应表达式不再触发暂停。

**待本地验证**：具体数值与是否需要加载特定镜像，取决于你的运行环境。若没有镜像，`$pc` 方案是最稳妥的最小验证。

## 6. 本讲小结

- **监视点 = 值变化的断点**：它监视一个表达式，值变化时暂停 CPU，是「追踪数据被谁修改」的关键工具。
- **静态对象池**：NEMU 用固定大小数组 `wp_pool[NR_WP]` 而非 `malloc`，规避了动态分配的复杂度，适合教学和热路径。
- **双链表管理**：`head` 串联「使用中」节点，`free_` 串联「空闲」节点；`init_wp_pool()` 把整块数组首尾相接编织成初始 `free_` 链，`head` 初始为空。
- **申请/释放即搬运**：`new_wp` 把节点从 `free_` 头搬到 `head` 头，`free_wp` 反之，全程 O(1)，节点总数守恒。
- **每步检查 + 状态机协作**：监视点记录表达式与旧值，`execute()` 每步后用 `expr()` 重算并比较；变化时把 `nemu_state.state` 置为 `NEMU_STOP`，循环检测到非 `NEMU_RUNNING` 即 `break`，控制权回到 SDB。
- **模块串联**：本讲是 u2-l6/u2-l7（表达式）的「消费者」，也是 u3-l9（`execute` 循环）的扩展点，三者通过 `expr()` 与 `nemu_state` 串成完整的调试闭环。

## 7. 下一步学习建议

- **进入 PA2 的 CPU 实现**：监视点能暂停 CPU，接下来要深入「CPU 每步到底做了什么」。建议学习 u3-l9「CPU 执行主循环」，你会在 [src/cpu/cpu-exec.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c) 里再次看到本讲插入 `test_watchpoints()` 的那个 `execute()` 循环，从更底层理解 `exec_once`、`trace_and_difftest` 与 `g_nr_guest_inst`。
- **继续阅读源码**：
  - 对照 [src/cpu/cpu-exec.c:100-128](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L100-L128) 的 `cpu_exec` 状态分支，确认 `NEMU_STOP` 与 `NEMU_END`/`NEMU_ABORT` 的不同处理路径。
  - 思考：监视点检查放在 `exec_once` 之后、`break` 判断之前，与放在循环开头（取指之前）有何语义差异？（提示：暂停点的 `pc` 是「刚执行完的那条」还是「将要执行的那条」？）
- **可选拓展**：实现 `info w` 命令列出所有监视点；或为监视点支持「条件」（如 `w $a0 > 10`），体会它与表达式求值、状态机的进一步组合。
