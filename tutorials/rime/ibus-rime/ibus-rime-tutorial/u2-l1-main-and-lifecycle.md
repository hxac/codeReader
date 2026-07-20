# main 入口与进程生命周期

## 1. 本讲目标

读完本讲，你应当能够：

- 说出 `main()` 里三行关键代码各做了什么，以及为什么真正的启动逻辑放在 `rime_with_ibus()` 里。
- 解释 `SIGTERM` / `SIGINT` 到来时，程序为什么不直接 `exit()`，而是走 `sigterm_cb → ibus_quit()` 这条「优雅退出」路径。
- 说清全局指针 `rime_api` 是什么、由谁创建、在哪里被使用。
- 把 `ibus_rime_start()` / `ibus_rime_stop()` 这对前端函数，与 librime 的 `initialize()` / `finalize()` 生命周期一一对应起来。

本讲只聚焦「进程怎么起来、怎么活着、怎么安全退下」这一条主线；IBus 总线连接、引擎注册、部署通知等细节留到后续讲义（u2-l2、u2-l3）。

## 2. 前置知识

本讲默认你已经读过 **u1-l3（目录结构与源码地图）**，知道：

- 仓库根目录有三个 C 文件，其中 [rime_main.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c) 是「入口层」，负责进程启动与生命周期。
- 最终编译产物是可执行文件 `ibus-engine-rime`，由 IBus 守护进程带 `--ibus` 参数拉起。
- ibus-rime 是「薄前端」，真正的输入法算法在 librime（核心引擎）里，前端通过一个叫 `RimeApi` 的结构体与核心通信。

下面几个基础概念在本讲会反复出现，先一句话澄清：

- **信号（signal）**：Linux 内核通知进程发生某事件的一种软中断。比如你在终端按 `Ctrl+C`，内核就给进程发 `SIGINT`；用 `kill <pid>` 默认发的是 `SIGTERM`。进程可以注册一个回调函数来「捕获」这些信号，而不是被默认行为（通常是被杀掉）处理。
- **主循环（main loop）**：IBus / GLib 这类框架都是「事件驱动」的。进程启动后进入一个无限循环，不断等待并处理事件（按键、总线消息、定时器……），直到有人请求它退出。`ibus_main()` 就是进入这个循环。
- **生命周期函数**：指成对出现的「初始化 / 清理」函数，比如 `initialize()` 分配资源、`finalize()` 释放资源。它们必须配对使用，否则会泄漏资源或丢失未写盘的数据。

## 3. 本讲源码地图

本讲几乎只看一个文件，外加一个构建期生成的头文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [rime_main.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c) | 进程入口层 | `main`、`sigterm_cb`、`rime_api`、`ibus_rime_start`、`ibus_rime_stop`、`rime_with_ibus` |
| [rime_config.h.in](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_config.h.in) | 构建模板，被 CMake 加工成 `rime_config.h` | 提供 `IBUS_RIME_VERSION`、`IBUS_RIME_SHARED_DATA_DIR` 等宏 |

> 提示：`rime_config.h` 不在源码树里，它是构建时由 `rime_config.h.in` 生成的（见 u1-l2）。你在源码里只能看到 `.in` 模板。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① `main` 函数，② 信号处理 `sigterm_cb`，③ `rime_api` 全局指针，④ `ibus_rime_start` / `ibus_rime_stop` 的 initialize/finalize 生命周期。最后用一个「完整调用链」把它们串起来。

### 4.1 main 函数：整个进程的起点

#### 4.1.1 概念说明

C 程序的入口永远是 `main()`。对 ibus-rime 来说，`main()` 本身故意写得很短——它只做三件「必须在最早期完成」的事，然后把舞台交给 `rime_with_ibus()`。这样设计的好处是：一眼就能看出「进程级的前置准备」和「业务级的工作流程」是分开的。

#### 4.1.2 核心流程

`main()` 的执行顺序可以用下面这段伪代码概括：

```
main(argc, argv)
  1. 注册信号回调：SIGTERM、SIGINT → sigterm_cb
  2. rime_api = rime_get_api()        # 拿到 librime 的 API 句柄
  3. rime_with_ibus()                 # 真正的启动逻辑全在这里
  4. return 0
```

注意第 1、2 步的顺序：先装好信号处理，再获取 API，最后才进入主流程。这样即便启动过程中收到终止信号，也已经有办法优雅处理。

#### 4.1.3 源码精读

[rime_main.c:L143-L150](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L143-L150) 是整个程序的入口：

```c
int main(gint argc, gchar** argv) {
  signal(SIGTERM, sigterm_cb);
  signal(SIGINT, sigterm_cb);

  rime_api = rime_get_api();
  rime_with_ibus();
  return 0;
}
```

- `signal(SIGTERM, sigterm_cb);` 和 `signal(SIGINT, sigterm_cb);`：把两种终止信号都交给同一个回调 `sigterm_cb` 处理（见 4.2）。
- `rime_api = rime_get_api();`：从 librime 取回它的 API 结构体指针，存到全局变量（见 4.3）。
- `rime_with_ibus();`：真正干活的函数，连接 IBus、注册引擎、启动 librime、进入主循环，全部在这里完成（见 4.5）。

> 细节：`main` 用的是 GLib 的类型 `gint` / `gchar**` 而不是 `int` / `char**`，因为整个项目重度依赖 GLib。另外 `argv` 在这里**没有被解析**——如 u1-l3 所述，`--ibus` 只是 IBus 拉起进程时的约定标记，`main()` 并不读它。

#### 4.1.4 代码实践

**实践目标**：在源码上标注 `main()` 的三步动作。

**操作步骤**：

1. 打开 [rime_main.c:L143](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L143)。
2. 在第 144、147、148 行旁边分别用注释标上 `①装信号`、`②取 API`、`③进主流程`（仅本地修改，用于自学，勿提交）。

**需要观察的现象**：你会发现 `main` 函数体里**没有任何**直接的 IBus / librime 调用（没有 `ibus_bus_new`、没有 `initialize`），它们都被封装到了 `rime_with_ibus()` 里。

**预期结果**：`main` 看起来「什么都没干」，这正是「入口层只做编排、不做具体业务」的体现。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `signal(SIGTERM, sigterm_cb);` 这两行删掉，会发生什么？

**答案**：进程将使用系统默认的信号处理行为。对 `SIGTERM` 和 `SIGINT`，默认行为是「终止进程」，于是进程会被立刻杀掉，**来不及执行 `ibus_main()` 之后的清理代码**（即 `ibus_rime_stop()` / `finalize()` 不会跑），librime 内部未写盘的数据可能丢失。这正是本讲要强调的「优雅退出」的意义。

---

### 4.2 信号处理 sigterm_cb：优雅退出

#### 4.2.1 概念说明

「优雅退出」（graceful shutdown）是指：收到终止信号后，程序不立刻消失，而是先把正在做的事做完、把占用的资源释放、把该存的数据存好，再正常退出。对输入法引擎来说这很重要——用户词库、会话状态都在内存里，强杀可能导致数据损坏。

`sigterm_cb` 就是实现优雅退出的关键一环。它的策略很巧妙：**不在信号回调里直接清理，而是只「请求主循环退出」**，真正的清理留给主线程在正常流程中完成。

#### 4.2.2 核心流程

为什么不在信号回调里直接调 `finalize()`？因为信号回调运行在一个特殊的「信号上下文」里，它可以在**任意时刻**打断主线程正在执行的任何代码——包括 librime 正在操作内部数据结构的瞬间。如果在回调里直接调用 librime 的清理函数，就可能和主线程正在进行的操作冲突，造成死锁或数据损坏。

通用的安全做法是：信号回调只做一件极轻的事——通知主循环「该退了」。主循环收到通知后正常退出，控制权回到 `rime_with_ibus()` 的下一行，于是清理代码在**主线程的常规流程**里顺序执行，不会有并发冲突。

信号安全性的形式化直觉可以这么表达：信号回调里只应调用「异步信号安全（async-signal-safe）」的函数。用集合记号描述：

\[
\text{允许在回调中调用} \;\subseteq\; \text{async-signal-safe 函数集}
\]

`exit()` 虽然属于这个集合，但会跳过后续清理；而 librime 的 `finalize()` 明显**不属于**这个集合。所以本项目的选择是调用 `ibus_quit()`——一个只触发「主循环退出」的轻动作——把不安全的清理工作推迟到主循环之外。

#### 4.2.3 源码精读

[rime_main.c:L138-L141](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L138-L141) 是信号回调本身：

```c
static void sigterm_cb(int sig) {
  // Notify the main program to exit.
  ibus_quit();
}
```

- 它什么都不碰，只调用 `ibus_quit()`。注释 `// Notify the main program to exit.` 把意图说得很清楚——「通知主程序退出」，而不是「自己退出」。
- `ibus_quit()` 的作用是让 `ibus_main()` 正在跑的那个 GLib 主循环结束（下一节会看到它在第 129 行）。一旦主循环结束，`ibus_main()` 就会返回，程序流继续往下走，走到 `ibus_rime_stop()`。

所以「SIGTERM 到来时如何安全退出 librime」的完整链路是：

```
内核交付 SIGTERM/SIGINT
  → sigterm_cb(sig) 被调用              # 在信号上下文中
      → ibus_quit()                     # 只请求主循环退出，不直接清理
  → ibus_main() 主循环结束并返回          # 控制权回到主线程常规流程
  → rime_with_ibus() 继续往下走
      → ibus_rime_stop()                # 见 4.4
          → rime_api->finalize()        # 这里才真正安全地清理 librime
      → notify_uninit() / g_object_unref(...)
  → rime_with_ibus() 返回
  → main() return 0                     # 进程正常退出
```

#### 4.2.4 代码实践

**实践目标**：验证「信号回调只触发退出、清理发生在主循环之后」这一设计。

**操作步骤**：

1. 阅读 [rime_main.c:L138-L141](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L138-L141) 与 [rime_main.c:L129-L132](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L129-L132)。
2. 想象（或本地实现）一个对照实验：把 `sigterm_cb` 里的 `ibus_quit();` 换成 `rime_api->finalize(); exit(0);`，然后回答下方问题。

**需要观察的现象**：对比两种写法下，`ibus_main()` 之后的 `ibus_rime_stop()`、`notify_uninit()`、`g_object_unref(factory)`、`g_object_unref(bus)` 是否还会被执行。

**预期结果**：原实现中这些清理语句会执行；改成 `exit(0)` 后它们**全部被跳过**。这能直观说明为什么作者选择「只通知、不直接退」。实际运行需要桌面 IBus 环境，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`sigterm_cb` 的参数 `int sig` 在函数体里被使用了吗？为什么还要保留它？

**答案**：没有使用。但 `signal()` 注册的回调函数签名要求是 `void handler(int)`，所以必须带上这个参数，哪怕不用。它表示「触发本次回调的信号编号」（`SIGTERM` 或 `SIGINT`），本项目两种信号共用同一个处理逻辑，所以忽略了具体值。

**练习 2**：为什么 `SIGTERM` 和 `SIGINT` 共用 `sigterm_cb`，而不是各写一个？

**答案**：因为期望的行为完全一样——都是「请求主循环退出，然后走清理流程」。两种信号只是来源不同（`SIGINT` 通常来自终端 `Ctrl+C`，`SIGTERM` 通常来自 `kill` 命令或桌面会话管理），处理方式无需区分，复用一个回调更简洁。

---

### 4.3 rime_api 全局指针：连接核心引擎

#### 4.3.1 概念说明

ibus-rime 是「薄前端」，自己不含任何输入法算法。所有跟算法相关的操作（创建会话、处理按键、查词、部署方案……）都要委托给 librime。问题是：前端怎么调用 librime？

答案就是 `RimeApi` 这个结构体。librime 把自己所有能力以「函数指针」的形式塞进一个 `RimeApi` 结构体里，前端拿到这个结构体的指针，就能通过 `rime_api->initialize(...)`、`rime_api->process_key(...)` 这样的方式调用核心能力。`RimeApi` 是 librime 对外承诺的**稳定边界**——只要这个结构体的布局不破坏兼容，librime 内部可以随便重写，前端无需改代码。这也是「一处实现核心，多处编写前端」架构的物理落点（见 u1-l1）。

#### 4.3.2 核心流程

```
librime（核心，C++ 共享库）
   │  对外暴露 rime_get_api()
   ▼
rime_get_api()  ──返回──▶  RimeApi *  ──赋值──▶  全局 rime_api
                                                       │
                                          前端各处用 rime_api->xxx() 调用核心
```

`rime_api` 被定义成一个**全局变量**，是因为它要在多个文件之间共享：`rime_main.c` 里初始化、`rime_engine.c` 处理按键时用、`rime_settings.c` 读配置时也用。用全局变量是最直接的共享方式（项目体量小，不必引入更复杂的依赖注入）。

#### 4.3.3 源码精读

[rime_main.c:L23](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L23) 定义了这个全局指针：

```c
RimeApi *rime_api = NULL;
```

- 初值为 `NULL`，表示「还没拿到 API」。
- 它在 [rime_main.c:L147](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L147) 被 `main()` 填上真实值：

```c
rime_api = rime_get_api();
```

- `rime_get_api()` 是 librime 对外暴露的入口函数（声明在 librime 的 `rime_api.h` 中，本项目通过 `#include <rime_api.h>` 引入，见 [rime_main.c:L12](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L12)）。它返回一个指向 `RimeApi` 结构体的指针，里面是一组函数指针（`initialize`、`finalize`、`setup`、`start_maintenance`、`deploy_config_file`、`set_notification_handler` 等）。
- 本文件里后续所有对 librime 的调用，都走 `rime_api->` 这条路，例如：
  - [rime_main.c:L76](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L76) `rime_api->initialize(&ibus_rime_traits);`
  - [rime_main.c:L85](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L85) `rime_api->finalize();`
  - [rime_main.c:L123](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L123) `rime_api->setup(&ibus_rime_traits);`

> 说明：librime 以 git 子模块形式存在于仓库的 `librime/` 目录（见 u1-l3），其头文件 `rime_api.h` 提供了 `RimeApi` 的完整定义。本环境未检出该子模块，故不展开其内部字段，只依据 rime_main.c 中**实际调用**的方法名来讲解。

#### 4.3.4 代码实践

**实践目标**：确认 `rime_api` 是「一处定义、多处使用」的全局句柄。

**操作步骤**：

1. 在 [rime_main.c:L23](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L23) 看到它的定义与初值 `NULL`。
2. 在 [rime_main.c:L147](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L147) 看到它被赋值。
3. 用编辑器在 `rime_engine.c`、`rime_settings.c` 中搜索 `rime_api`，确认这两个文件也引用了它（它们通过 `extern` 声明引用同一个全局变量）。

**需要观察的现象**：`rime_api` 在 `rime_main.c` 中被定义并赋值，在另外两个引擎/设置文件中被使用，但**只读不改**。

**预期结果**：你会看到「入口层填一次、引擎层和设置层反复用」的共享模式，理解为什么用全局变量在这里是合理的。

#### 4.3.5 小练习与答案

**练习 1**：如果 `rime_get_api()` 返回 `NULL`（比如 librime 版本不兼容），本项目的代码会发生什么？

**答案**：从源码看，`main()` 并没有对返回值做非空检查，直接把可能为 `NULL` 的指针存进 `rime_api`，随后 `rime_with_ibus()` 在第 123 行调用 `rime_api->setup(...)` 时就会解引用空指针、导致段错误（segfault）退出。这属于「在版本不兼容时快速失败」的隐含约定——前端假设 librime 一定可用。`ibus_rime_stop()` 里反而有一处保护（见 4.4）。

---

### 4.4 ibus_rime_start / ibus_rime_stop：initialize / finalize 生命周期

#### 4.4.1 概念说明

librime 有自己的生命周期：先 `setup`（早期全局设置）→ 再 `initialize`（创建引擎实例、分配资源）→ 运行期各种调用 → 最后 `finalize`（释放资源、写盘）。前端需要把这几步包起来。

`ibus_rime_start()` 和 `ibus_rime_stop()` 就是 ibus-rime 对 librime 「初始化 / 清理」这一对的**前端封装**。把它们抽成独立函数有两个好处：

1. 让 `rime_with_ibus()` 的主流程读起来更清晰（`start → 主循环 → stop` 一目了然）。
2. 这对函数还能被「状态栏的部署按钮」复用——点击「部署」时，前端会先 `stop` 再 `start(TRUE)` 重启一次 librime（详见 u3-l3），相当于在不退出进程的前提下「重做一次 initialize/finalize」。如果这两步直接写死在主流程里，就没法复用了。

#### 4.4.2 核心流程

`ibus_rime_start(full_check)` 的内部流程：

```
ibus_rime_start(full_check)
  ├── 计算 user_data_dir = ~/.config/ibus/rime
  ├── 若该目录不存在 → g_mkdir_with_parents(权限 0700)
  ├── 构造 RimeTraits（RIME_STRUCT 零初始化）
  │     ├── fill_traits(...)            # shared_data_dir / distribution_*  / app_name
  │     └── user_data_dir = 上面算出的路径
  ├── rime_api->initialize(&traits)     # ← librime 初始化
  └── if rime_api->start_maintenance(full_check):
          rime_api->deploy_config_file("ibus_rime.yaml", "config_version")
```

`ibus_rime_stop()` 的内部流程：

```
ibus_rime_stop()
  └── if rime_api != NULL:
          rime_api->finalize()          # ← librime 清理
```

两者与 librime 生命周期的对应关系：

| 前端函数（rime_main.c） | librime API 调用 | 阶段 |
| --- | --- | --- |
| `rime_with_ibus()` 第 123 行 | `rime_api->setup()` | 早期全局设置（在 start 之前） |
| `ibus_rime_start()` 第 76 行 | `rime_api->initialize()` | 创建实例、分配资源 |
| `ibus_rime_start()` 第 77 行 | `rime_api->start_maintenance()` | 触发部署/维护检查 |
| `ibus_rime_start()` 第 79 行 | `rime_api->deploy_config_file(...)` | 刷新前端配置 |
| `ibus_rime_stop()` 第 85 行 | `rime_api->finalize()` | 释放资源、写盘 |

#### 4.4.3 源码精读

先看 [rime_main.c:L25-L30](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L25-L30)，它算出用户数据目录：

```c
static const char* get_ibus_rime_user_data_dir(char *path) {
  const char* home = getenv("HOME");
  strcpy(path, home);
  strcat(path, "/.config/ibus/rime");
  return path;
}
```

- 用户级数据（方案、用户词库、构建缓存）放在 `~/.config/ibus/rime` 下。

然后是 [rime_main.c:L66-L81](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L66-L81) 的 `ibus_rime_start`：

```c
void ibus_rime_start(gboolean full_check) {
  char user_data_dir[512] = {0};
  get_ibus_rime_user_data_dir(user_data_dir);
  if (!g_file_test(user_data_dir, G_FILE_TEST_IS_DIR)) {
    g_mkdir_with_parents(user_data_dir, 0700);
  }
  RIME_STRUCT(RimeTraits, ibus_rime_traits);
  fill_traits(&ibus_rime_traits);
  ibus_rime_traits.user_data_dir = user_data_dir;

  rime_api->initialize(&ibus_rime_traits);
  if (rime_api->start_maintenance((Bool)full_check)) {
    // update frontend config
    rime_api->deploy_config_file("ibus_rime.yaml", "config_version");
  }
}
```

逐行解读：

- `char user_data_dir[512] = {0};` + `get_ibus_rime_user_data_dir(...)`：在栈上开一个 512 字节的缓冲区，填入用户目录路径。
- `g_file_test(...) / G_FILE_TEST_IS_DIR`：用 GLib 检查目录是否已存在；不存在则 `g_mkdir_with_parents(user_data_dir, 0700)` 创建（`0700` 表示仅属主可读写执行，保护用户词库隐私）。
- `RIME_STRUCT(RimeTraits, ibus_rime_traits);`：这是 librime 提供的宏，作用是「按 `RimeTraits` 结构体的大小零值分配并初始化一个栈变量」。用宏而不是直接 `RimeTraits ibus_rime_traits = {0};` 是为了兼容不同 librime 版本下结构体长度不一致的情况——宏内部会按版本正确处理尺寸。
- `fill_traits(&ibus_rime_traits);`：填充共享数据目录、发行版信息、app 名（见下方）。
- `ibus_rime_traits.user_data_dir = user_data_dir;`：再把用户目录补上（`fill_traits` 不负责它）。
- `rime_api->initialize(&ibus_rime_traits);`：**关键一行**——调用 librime 的 `initialize`，把 traits 传进去，librime 据此创建引擎实例。
- `rime_api->start_maintenance((Bool)full_check)`：启动「维护」流程（检查方案是否需要构建/部署）。返回非 0 表示「确实要做一次维护」，于是接着 `deploy_config_file("ibus_rime.yaml", "config_version")` 把前端配置文件也部署一遍。注意 `(Bool)full_check` 这个强制转换——GLib 的 `gboolean` 和 librime 的 `Bool` 是不同类型，需要显式转。

[rime_main.c:L58-L64](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L58-L64) 的 `fill_traits` 填的是「发行版身份」：

```c
static void fill_traits(RimeTraits *traits) {
  traits->shared_data_dir = IBUS_RIME_SHARED_DATA_DIR;
  traits->distribution_name = DISTRIBUTION_NAME;
  traits->distribution_code_name = DISTRIBUTION_CODE_NAME;
  traits->distribution_version = DISTRIBUTION_VERSION;
  traits->app_name = "rime.ibus";
}
```

- `IBUS_RIME_SHARED_DATA_DIR`、`DISTRIBUTION_VERSION`（即 `IBUS_RIME_VERSION`）来自构建期生成的 [rime_config.h.in:L4-L7](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_config.h.in#L4-L7)，值由 CMake 注入（见 u1-l2）。
- `DISTRIBUTION_NAME` / `DISTRIBUTION_CODE_NAME` 定义在 [rime_main.c:L19-L20](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L19-L20)，分别是 `"Rime"` 和 `"ibus-rime"`。

再看 [rime_main.c:L83-L87](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L83-L87) 的 `ibus_rime_stop`：

```c
void ibus_rime_stop() {
  if (rime_api) {
    rime_api->finalize();
  }
}
```

- 这里**有**一处 `if (rime_api)` 非空保护——与 4.3 练习里指出的「入口处未检查」形成对比。它确保即便 `rime_api` 为空（理论上 initialize 没成功），也不会在退出时空指针解引用。
- `rime_api->finalize()` 就是 librime 的清理：释放会话、刷写用户词库、回收内存。这就是 SIGTERM 最终触达的那一行（经 4.2 的优雅退出路径）。

#### 4.4.4 代码实践

**实践目标**：把前端 start/stop 与 librime 的 initialize/finalize 一一对应。

**操作步骤**：

1. 打开 [rime_main.c:L66-L81](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L66-L81)（`ibus_rime_start`）和 [rime_main.c:L83-L87](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L83-L87)（`ibus_rime_stop`）。
2. 在源码边上的笔记本里画一张两列对照表：左列「前端动作」，右列「对应的 librime 调用 + 行号」。
3. （可选，本地学习用）在 `ibus_rime_start` 的 `rime_api->initialize(...)` 之后加一行 `g_message("librime initialized, full_check=%d", full_check);`，在 `ibus_rime_stop` 的 `finalize()` 之前加一行 `g_message("librime finalizing");`，重新编译运行，观察日志顺序。

**需要观察的现象**：启动时先看到「initialized」，进程退出（被发 SIGTERM）时才看到「finalizing」——证明 finalize 发生在主循环结束之后。

**预期结果**：日志顺序为 `initialized` →（运行期）→ `finalizing`，正对应「start → ibus_main 阻塞 → 信号触发退出 → stop」的生命周期。本实践需在桌面 IBus 环境下运行 `ibus-engine-rime`，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`rime_api->setup()` 和 `rime_api->initialize()` 谁先执行？分别在哪个函数里？

**答案**：`setup()` 先执行，位于 `rime_with_ibus()` 第 [123 行](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L123)；`initialize()` 后执行，位于 `ibus_rime_start()` 第 [76 行](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L76)（而 `ibus_rime_start` 是在第 [126 行](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L126) 才被调用的）。`setup` 用于更早期的全局设置，`initialize` 才真正创建引擎实例。

**练习 2**：`ibus_rime_start` 的参数 `full_check` 传 `TRUE` 和 `FALSE` 有什么区别？

**答案**：它被原样（经类型转换）传给 `rime_api->start_maintenance((Bool)full_check)`（第 [77 行](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L77)）。进程正常启动时传 `FALSE`（第 [125 行](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L125)），表示「只在必要时才维护」；而「部署」按钮触发时传 `TRUE`，强制做一次完整检查与重建。无论哪种，只有当 `start_maintenance` 返回真值（确实执行了维护）时，才会接着 `deploy_config_file` 刷新 `ibus_rime.yaml`。

**练习 3**：为什么 `ibus_rime_stop` 里有 `if (rime_api)`，而 `main` 里赋值后却没有检查就用？

**答案**：`main` 里假设 librime 一定可用（版本匹配的部署环境下成立），失败即段错误快速退出，属于隐含约定；而 `ibus_rime_stop` 是退出路径，必须防御性编程——万一 `rime_api` 因某种原因为空（例如未来加入初始化失败处理），`finalize` 调用不应再制造一次空指针崩溃。退出路径上加保护是低成本、高收益的稳健写法。

---

### 4.5 串起来：从 main() 到 ibus_main() 的完整调用链

把前面四个模块连起来，就得到本讲实践任务要标注的那条完整链路。下面是 [rime_main.c:L94-L136](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L94-L136) 中 `rime_with_ibus()` 的主流程（本讲只关心生命周期相关行，IBus 总线/工厂部分留给 u2-l2）：

```
main(argc, argv)                                       # L143
  ├── signal(SIGTERM/SIGINT → sigterm_cb)              # L144-L145   ①装信号
  ├── rime_api = rime_get_api()                        # L147        ②取 API
  └── rime_with_ibus()                                 # L148 / L94  ③进主流程
        ├── ibus_init() / ibus_bus_new() ...           # L95-L118    连总线+注册（u2-l2）
        ├── notify_init / set_notification_handler     # L115-L119
        ├── fill_traits + rime_api->setup(...)         # L121-L123   librime 早期设置
        ├── ibus_rime_start(FALSE)                     # L125-L126   ← initialize + maintenance
        ├── ibus_rime_load_settings()                  # L127        读 ibus_rime.yaml
        ├── ibus_main()                                # L129        ★ 进入主循环，阻塞
        ├── ibus_rime_stop()                           # L131        ← finalize
        ├── notify_uninit()                            # L132
        └── g_object_unref(factory) / unref(bus)       # L134-L135
```

关键时序点：

1. **第 123 行 `setup`** 是 librime 最早被调到的方法，比 `initialize` 还早。
2. **第 126 行 `ibus_rime_start(FALSE)`** 内部完成 `initialize` + 可能的 `deploy_config_file`。
3. **第 129 行 `ibus_main()`** 是分水岭——进程在这里**阻塞**，进入事件循环，等待按键、总线消息等事件。正常运行时，进程就「停」在这里。
4. **第 131 行 `ibus_rime_stop()`** 只有在 `ibus_main()` 返回之后才会执行——也就是收到 SIGTERM/SIGINT（或 IBus 总线断开，见 u2-l2）触发 `ibus_quit()` 之后。它内部调用 `finalize`，安全收尾。

所以「SIGTERM 到来时如何安全退出 librime」的完整答案可以浓缩成一句话：**信号回调只通过 `ibus_quit()` 请求主循环退出，真正的 `rime_api->finalize()` 在 `ibus_main()` 返回后、由主线程在 `ibus_rime_stop()` 中常规执行，从而避免了信号上下文里并发操作 librime 的风险。**

## 5. 综合实践

**任务**：画一张「ibus-rime 进程生命周期时序图」，并标注每一行源码位置。

要求：

1. 横轴是时间，从「IBus 守护进程拉起 `ibus-engine-rime`」开始，到「进程退出」结束。
2. 在时间轴上标出下列事件，每个事件旁注明对应的 [rime_main.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c) 行号：
   - 信号回调注册
   - 获取 `rime_api`
   - librime `setup`
   - librime `initialize`
   - `start_maintenance` / `deploy_config_file`
   - 进入 `ibus_main()` 主循环（标注「阻塞」段）
   - 收到 SIGTERM → `ibus_quit()`
   - librime `finalize`
   - `main` 返回 0
3. 用不同颜色（或实线/虚线）区分「主线程常规流程」与「信号触发的异步路径」。
4. 在图下用一段话解释：为什么 `finalize` 必须画在「主循环退出之后」，而不能画在「信号回调里」。

**参考答案要点**：

- 信号注册（L144-L145）→ 取 API（L147）→ setup（L123）→ initialize（L76，在 `ibus_rime_start` 内）→ maintenance/deploy（L77-L79）→ `ibus_main` 阻塞（L129）。
- 异步路径：SIGTERM → sigterm_cb（L138-L141）→ `ibus_quit()`（L140）→ 使 `ibus_main()` 返回。
- 主线程继续：`ibus_rime_stop()`（L131）→ `finalize()`（L85）→ 返回 0（L149）。
- 解释：信号回调运行在异步信号上下文，可能与主线程正在进行的 librime 操作冲突；推迟到主循环退出后由主线程顺序执行 `finalize`，才是并发安全的清理方式。

## 6. 本讲小结

- `main()` 只做三件事：注册终止信号回调、用 `rime_get_api()` 拿到 librime 的 API 句柄、调用 `rime_with_ibus()` 进入主流程。
- `sigterm_cb` 的精髓是「只通知、不直接退出」——调用 `ibus_quit()` 请求主循环结束，把真正的清理留给主线程。
- 全局指针 `rime_api` 是前端与 librime 核心之间的稳定边界，在 `main` 中赋值、在引擎/设置层多处复用。
- `ibus_rime_start()` / `ibus_rime_stop()` 是对 librime `initialize()` / `finalize()` 的前端封装，成对出现、可被部署按钮复用。
- 完整链路是：`main → rime_with_ibus → setup → ibus_rime_start(initialize) → ibus_main(阻塞) → 信号触发 ibus_quit → ibus_rime_stop(finalize) → 返回`。
- 这种「信号请求退出 + 主线程顺序清理」的设计，是输入法引擎避免数据损坏、实现优雅退出的关键。

## 7. 下一步学习建议

本讲只追到了 `rime_with_ibus()` 里与生命周期直接相关的行，故意跳过了第 95-118 行的 IBus 连接与引擎注册。下一步建议：

- **u2-l2（IBus 总线、工厂与引擎注册）**：精读 `rime_with_ibus()` 的前半段，搞清 `ibus_init` / `ibus_bus_new` / `ibus_bus_is_connected` / `ibus_factory_new` / `ibus_factory_add_engine` / `ibus_bus_request_name` 这一串调用，以及 `ibus_disconnect_cb` 这第二条触发 `ibus_quit()` 的退出路径。
- **u2-l3（librime 初始化、部署与通知）**：深入 `notification_handler`、`start_maintenance`、`deploy_config_file`，理解部署成功后为何要回调 `ibus_rime_load_settings()`，以及用户目录 `~/.config/ibus/rime` 的创建时机（本讲已点到，u2-l3 会展开）。

阅读时建议把本讲的「生命周期时序图」放在手边，后续讲义里所有 IBus/librime 调用都可以对回到这张图的某个时间点上。
