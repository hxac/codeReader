# 实战：用 rime_api_console 体验输入流程

## 1. 本讲目标

上一篇 u1-l4 我们看清了 librime 的 C API「方法表」`RimeApi`，知道它把所有能力都暴露成函数指针。但那张表读起来偏抽象——这一篇我们就用一个**真实可运行的程序**把它跑起来。

仓库自带了一个命令行示例 `tools/rime_api_console.cc`，它是一个最小却完整的「前端」：不画界面，只在终端里接收按键、打印候选、提交文字。本讲学完后你应该能够：

- 看懂 console 程序从 `setup` 到 `finalize` 的**完整生命周期**，并说出每一步对应 `RimeApi` 里的哪个函数指针。
- 理解 librime 给前端的三种核心输出结构 `RimeCommit` / `RimeStatus` / `RimeContext` 各自装了什么，以及为什么它们总是 `get_*` 与 `free_*` **成对出现**。
- 掌握 `simulate_key_sequence`、`select_candidate`、`set_option`、`select_schema` 等**交互 API** 的用途。
- 亲手编译并运行 `rime_api_console`，在终端里体验「按键 → 候选 → 提交」的完整链路。

> 本讲是**入门层的最后一篇**。它把前几讲讲过的「引擎定位 / 构建产物 / 目录结构 / C API 入口」串成一个可操作的整体，为后面 u2 开始的运行时对象深入打下直觉。

## 2. 前置知识

在动手之前，先用一句话回顾几个概念（详细版见 u1-l1 ~ u1-l4）：

- **引擎 vs 前端**：librime 是引擎，只负责「按键 → 文字」的计算；前端负责画候选窗、对接操作系统的输入法框架。`rime_api_console` 就是**用 C API 写出的最朴素前端**。
- **会话（Session）**：一次输入法交互（比如一个输入框焦点）对应一个会话，用 `RimeSessionId` 标识。多个会话互不干扰，由 `Service` 统一管理（u2-l2 会详讲）。
- **方案（Schema）**：一个 YAML 描述的输入法（拼音、仓颉……）。切换方案就是换输入法，引擎不变。
- **自版本化结构体**：跨 C 边界的结构体首字段是 `data_size`，用 `RIME_STRUCT(T, var)` 宏初始化，库据此判断字段是否存在（u1-l4 已讲）。本讲会反复用到这个宏。

如果上面这些词你还觉得陌生，建议先读 u1-l4 再回来。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `tools/rime_api_console.cc` | 命令行示例「前端」 | 全文：生命周期、主循环、打印输出、交互命令、通知回调 |
| `tools/line_editor.h` / `.cc` | 终端行编辑器（历史、光标、提示） | 只需知道它负责「读一行输入」，细节不展开 |
| `tools/codepage.h` | Windows 控制台 UTF-8 设置 | 跨平台小工具，非重点 |
| `src/rime_api.h` | C API 定义 | `RimeApi` 方法表、`RimeTraits`、三种输出结构、`RIME_STRUCT` 宏 |

构建方面，`tools/CMakeLists.txt` 会把最小数据集（`default.yaml`、`luna_pinyin.*`、`cangjie5.*` 等）拷贝到可执行文件输出目录，这一点对后面的实践很关键。

## 4. 核心概念与源码讲解

我们把 `rime_api_console.cc` 这个 270 行的程序拆成 5 个最小模块，按「程序执行顺序」逐一拆解。

### 4.1 程序生命周期：从 setup 到 finalize

#### 4.1.1 概念说明

任何一个想用 librime 的前端程序，都要走一条固定的「开机 → 工作 → 关机」流程：

1. **setup（全局设置）**：告诉引擎「我是谁」（`app_name`）、数据放哪、日志怎么打。这一步只设参数，**还不加载任何东西**。
2. **initialize（初始化）**：真正加载模块（core/dict/gears）、启动 `Service`。到这一步引擎才「活」过来。
3. **start_maintenance（维护/部署）**：第一次运行时，引擎要检查数据是否需要编译（把 `.dict.yaml` 编译成 `.bin`）。这一步可能较慢，因此放在后台线程，主线程用 `join_maintenance_thread` 等它做完。
4. **工作循环**：创建会话、处理按键、读取输出。
5. **finalize（终止）**：销毁会话、停止 `Service`、清理注册表与模块。

这五步在 `RimeApi` 表里分别对应 `setup` / `initialize` / `start_maintenance` + `join_maintenance_thread` / `create_session` / `finalize` 等函数指针。

#### 4.1.2 核心流程

```text
main()
  ├── SetConsoleOutputCodePage()      # Windows 下设 UTF-8（Linux 上为空操作）
  ├── rime_get_api()                  # 拿到全局唯一的 RimeApi 方法表
  ├── RIME_STRUCT(RimeTraits, traits) # 声明一个自版本化结构体
  ├── rime->setup(&traits)            # ① 全局设置
  ├── rime->set_notification_handler()# ② 注册通知回调
  ├── rime->initialize(NULL)          # ③ 加载模块、StartService
  ├── rime->start_maintenance(True)   # ④ 触发部署检查
  │      └── rime->join_maintenance_thread()  #    等待部署完成
  ├── ... 主循环（见 4.2） ...
  ├── rime->destroy_session(...)      # ⑤ 销毁会话
  └── rime->finalize()                #    终止引擎
```

#### 4.1.3 源码精读

`main` 的开头完成了「开机」前四步：

[tools/rime_api_console.cc:219-235](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L219-L235) —— 拿到 API 表、用 `RIME_STRUCT` 声明 `RimeTraits` 并填入 `app_name = "rime.console"`，然后依次 `setup` → 注册回调 → `initialize` → 维护等待。

这里有两个细节值得注意：

- `RIME_STRUCT(RimeTraits, traits);` 会把结构体清零并把 `data_size` 设成 `sizeof(RimeTraits) - sizeof(int)`，让库知道这份 traits 是「哪个版本」的（u1-l4 讲过的自版本化机制）。
- console **只设了 `app_name`**，没有设 `shared_data_dir` / `user_data_dir`。这时它们保持默认（空路径），资源解析会退化为相对路径——这正是为什么稍后我们要在数据文件所在目录运行程序。

`RimeTraits` 结构定义见 [src/rime_api.h:84-117](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L84-L117)，字段按版本分组（v0.9 / v1.0 / v1.6），能清楚看到 `shared_data_dir`、`user_data_dir`、`app_name`、`modules`、`min_log_level`、`log_dir`、`prebuilt_data_dir`、`staging_dir` 等。

对应的 `RimeApi` 表里这几组函数指针见 [src/rime_api.h:262-293](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L262-L293)：`setup`、`set_notification_handler`、`initialize`、`finalize`、`start_maintenance`、`is_maintenance_mode`、`join_maintenance_thread`。

「关机」两步在 `main` 末尾：

[tools/rime_api_console.cc:265-270](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L265-L270) —— 先 `destroy_session` 再 `finalize`，最后恢复控制台代码页。

#### 4.1.4 代码实践

**目标**：在不运行的情况下，仅靠阅读源码确认「开机四步」对应 `RimeApi` 的哪些函数指针。

**步骤**：

1. 打开 `src/rime_api.h`，定位 `RimeApi` 结构体（约 259 行起）。
2. 在其中找到 `setup`、`initialize`、`finalize`、`start_maintenance`、`join_maintenance_thread` 五个函数指针，记下它们的**签名**。
3. 对比 `rime_api_console.cc` 的 `main`，确认调用方式与签名一致（例如 `setup` 接收 `RimeTraits*`，而 `initialize` 同样接收 `RimeTraits*` 但 console 传了 `NULL`）。

**预期结果**：你会发现 `initialize(NULL)` 之所以能成立，是因为 `setup` 阶段已经把 traits 存进了引擎内部的 `Deployer`，再次传 traits 是可选的覆盖。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `rime->start_maintenance(full_check)` 的参数从 `True` 改成 `False`，行为会有什么不同？（提示：看 `src/rime_api_impl.h` 里的 `RimeStartMaintenance` 实现。）

> **答案**：`full_check = False` 时，引擎不会无条件重跑部署，而是先调用 `detect_modifications` 检测 `user_data_dir` 与 `shared_data_dir` 是否有改动，**有改动才**继续后续维护任务；`True` 则跳过检测直接进入维护。换言之 `False` 是「按需部署」，启动更快。

**练习 2**：`RIME_STRUCT(RimeTraits, traits)` 这个宏展开后做了哪两件事？

> **答案**：① 把变量零初始化（`Type var = {0};`）；② 调 `RIME_STRUCT_INIT` 把 `data_size` 设为 `sizeof(Type) - sizeof(data_size)`，从而完成自版本化登记。

---

### 4.2 会话与按键输入主循环

#### 4.2.1 概念说明

引擎「活」过来之后，真正干活的是**主循环**：它不断读取用户输入的一行文本，把它当作「按键序列」喂给某个会话。这里有三件事要理解：

- **会话的惰性创建**：循环里每次都先 `find_session` 检查当前会话是否还在，不在就 `create_session` 建一个。这样即便会话被意外销毁，程序也能自愈。
- **「一行 = 一次按键序列」**：`simulate_key_sequence` 接收一段字符串，内部把它解析成一串 `KeyEvent` 逐个 `process_key`，相当于「一次性按下这串键」。
- **特殊命令优先**：像 `select schema xxx`、`exit`、`reload` 这种以文本形式表达的命令，会在喂给按键序列**之前**被拦截处理。

#### 4.2.2 核心流程

```text
while (editor.ReadLine(&line)):           # 读一行
  if line 为空: line = "\r"               # 空行当作回车（提交）
  if find_session(id) 失败: id = create_session()
  if line == "exit": break
  if line == "reload": destroy+finalize, goto reload
  if execute_special_command(line): continue  # 4.4 讲
  if simulate_key_sequence(id, line):
      print(id)                            # 4.3 讲
  else:
      报错 "Error processing key sequence"
```

#### 4.2.3 源码精读

主循环本体见 [tools/rime_api_console.cc:237-263](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L237-L263)。注意三处：

- `if (line.empty()) line = "\r";`：空行被替换成回车符 `\r`，这样直接回车也会触发一次按键序列（通常是「确认首候选」）。
- `find_session` + `ensure_session` 的自愈逻辑，`ensure_session` 的实现见 [tools/rime_api_console.cc:211-217](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L211-L217)。
- 真正驱动引擎的一行：`rime->simulate_key_sequence(session_id, line.c_str())`，成功后调 `print(session_id)` 把三种输出结构打印出来。

会话管理三件套在 API 表里定义于 [src/rime_api.h:305-311](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L305-L311)：`create_session`、`find_session`、`destroy_session`、`cleanup_stale_sessions`、`cleanup_all_sessions`。`simulate_key_sequence` 与 `process_key` 定义于 [src/rime_api.h:313-318](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L313-L318) 和 [src/rime_api.h:372-375](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L372-L375)。

> 说明：`simulate_key_sequence` 是「测试用」便捷接口（API 注释里归在 `// testing` 分组），它在内部把字符串拆成按键再逐个 `process_key`。真实前端通常直接调更底层的 `process_key(session_id, keycode, mask)`（u2-l1 会讲 keycode/mask）。

#### 4.2.4 代码实践

**目标**：理解「一行输入 → 一次按键序列」的映射，并验证空行等于回车。

**步骤**：

1. 在 `rime_api_console.cc` 的 `while` 循环里找到 `simulate_key_sequence` 调用。
2. 思考：当你在终端输入 `ni` 然后回车，传给 `simulate_key_sequence` 的字符串是 `"ni"` 还是 `"ni\r"`？
3. 再思考：当你**直接按回车**（不输入任何字符），`line` 是空串，经过哪一行变成了 `"\r"`？

**预期结果**：你会确认——普通输入 `ni` 回车后传的是 `"ni"`（不含回车）；而空行回车经过 `if (line.empty()) line = "\r";` 变成 `"\r"`，相当于一个回车键，用于「确认/提交当前高亮候选」。

#### 4.2.5 小练习与答案

**练习 1**：为什么循环里每次都要 `find_session`，而不是只在程序开头 `create_session` 一次？

> **答案**：会话可能因为各种原因失效（例如 `reload` 命令会 `destroy_session` + `finalize` 后重建引擎）。每次循环先检查 `find_session`，失效就重建，保证程序对「会话消失」具备自愈能力，避免对一个已失效的 `session_id` 调用 `process_key`。

**练习 2**：`simulate_key_sequence` 返回 `False` 时程序怎么处理？这说明了什么设计原则？

> **答案**：打印 `Error processing key sequence` 到 `stderr`，但**不退出**循环。这说明引擎把「处理失败」当成可恢复的常态（比如输入了当前方案不接受的字符），前端应当提示用户而非崩溃。

---

### 4.3 三种输出结构：Commit / Status / Context

#### 4.3.1 概念说明

每处理完一次按键，前端需要从引擎「读取」当前状态。librime 给前端三种正交的输出结构：

| 结构 | 回答的问题 | 关键字段 | 配对 API |
| --- | --- | --- | --- |
| `RimeCommit` | 「这次有要**提交**（上屏）的文字吗？」 | `text` | `get_commit` / `free_commit` |
| `RimeStatus` | 「引擎现在处于什么**状态/模式**？」 | `schema_id`、`is_composing`、`is_ascii_mode`、`is_simplified`… | `get_status` / `free_status` |
| `RimeContext` | 「当前**输入串、候选菜单**长什么样？」 | `composition`（preedit/光标/选区）、`menu`（候选列表） | `get_context` / `free_context` |

为什么总是 `get_*` 配 `free_*`？因为 `text`、`preedit`、`candidates[].text` 这些都是引擎**分配在堆上的 C 字符串**，前端读完必须调对应的 `free_*` 归还，否则内存泄漏。这体现了一条 C 边界铁律：**谁分配，谁释放**。

#### 4.3.2 核心流程

console 用一个统一的 `print(session_id)` 函数依次拉取三种输出：

```text
print(session_id):
  RIME_STRUCT(RimeCommit,  commit)
  RIME_STRUCT(RimeStatus,  status)
  RIME_STRUCT(RimeContext, context)
  if get_commit(id, &commit):   printf("commit: %s", commit.text);  free_commit(&commit)
  if get_status(id, &status):   print_status(&status);              free_status(&status)
  if get_context(id, &context): print_context(&context);            free_context(&context)
```

`print_context` 又会进一步打印 `composition`（带 `[]` 标选区、`|` 标光标）和 `menu`（分页号 + 编号候选，高亮项用 `[]` 包裹）。

#### 4.3.3 源码精读

统一的 `print` 函数见 [tools/rime_api_console.cc:76-97](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L76-L97)，三段 `get_*` / `free_*` 严格成对。

三种结构的字段定义：

- `RimeCommit`（只有 `text`）：[src/rime_api.h:146-150](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L146-L150)
- `RimeStatus`（模式标志位集合）：[src/rime_api.h:165-180](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L165-L180)
- `RimeContext`（含 `RimeComposition` + `RimeMenu`）：[src/rime_api.h:155-163](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L155-L163)

其中 `RimeComposition` 的 `length` / `cursor_pos` / `sel_start` / `sel_end` / `preedit` 见 [src/rime_api.h:119-125](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L119-L125)；`RimeMenu` 的 `page_size` / `page_no` / `is_last_page` / `highlighted_candidate_index` / `candidates` 见 [src/rime_api.h:133-141](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L133-L141)。

`RimeCandidate` 只有 `text` 与 `comment` 两个字段（[src/rime_api.h:127-131](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L127-L131)），例如候选「你」可能有注释「(ni)」。

输出函数的实现值得一读：

- `print_status`：[tools/rime_api_console.cc:14-28](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L14-L28)，把 `is_composing` / `is_ascii_mode` / `is_full_shape` / `is_simplified` 等标志位拼成一行状态标签。
- `print_composition`：[tools/rime_api_console.cc:30-52](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L30-L52)，用 `[` `]` 包住 `[sel_start, sel_end)` 选区、用 `|` 标 `cursor_pos` 光标。
- `print_menu`：[tools/rime_api_console.cc:54-65](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L54-L65)，按页打印候选，最后一页用 `$` 标记，高亮候选用 `[ ]` 包裹。

对应的 `get_*` / `free_*` 函数指针在 API 表的 `// output` 分组：[src/rime_api.h:320-329](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L320-L329)。

#### 4.3.4 代码实践

**目标**：在源码层面走通「按键后读三种输出」的完整读取路径。

**步骤**：

1. 找到 `print` 函数（76 行起），数清楚它调用了几次 `get_*` 与几次 `free_*`。
2. 假设 `get_commit` 返回 `True`（说明有提交文字），跟踪 `commit.text` 被打印后是如何被释放的。
3. 思考：如果删掉 `rime->free_context(&context)` 这一行，连按很多次键后会发生什么？

**预期结果**：每轮循环 3 次 `get_*` 配 3 次 `free_*`（前提是该输出存在）。删掉 `free_context` 会导致引擎分配的 `preedit` 与候选字符串数组每轮都泄漏，长时间运行内存持续增长——这就是「成对调用」纪律存在的根本原因。

#### 4.3.5 小练习与答案

**练习 1**：`get_commit` 什么时候返回 `True`？返回 `False` 时 `commit.text` 还有效吗？

> **答案**：仅当本次按键导致有**待提交（上屏）**的文字时返回 `True`，此时 `commit.text` 指向引擎分配的字符串，前端读完要 `free_commit`。返回 `False` 表示没有新提交，此时不应使用 `commit.text`，也不需要释放。

**练习 2**：`RimeStatus` 里的 `is_composing` 和 `RimeContext.composition.length > 0` 表达的是同一件事吗？

> **答案**：高度相关但来源不同。`is_composing` 是引擎给出的「是否处于组词态」标志位；`composition.length > 0` 是从 context 里直接看「当前编辑区是否有内容」。console 在 `print_context` 里用 `length > 0 || num_candidates > 0` 来决定打印 preedit 还是 `(not composing)`（见 67-74 行），是一种等价的实用判断。

---

### 4.4 交互命令：方案切换、候选选择、开关设置

#### 4.4.1 概念说明

console 不只能「打字」。它内置了一组以文本形式表达的**特殊命令**（在 `execute_special_command` 里集中处理），让用户能像操作真实输入法一样：列方案、切方案、选候选、翻开关、同步用户数据、删候选。理解这些命令等于掌握了 `RimeApi` 里**会话级交互 API** 的全貌：

- **方案**：`get_schema_list` / `get_current_schema` / `select_schema`
- **候选**：`select_candidate_on_current_page` / `candidate_list_begin`+`next`+`end`（迭代器三件套）
- **开关（option）**：`set_option` / `get_option`
- **同步**：`sync_user_data`

#### 4.4.2 核心流程

```text
execute_special_command(line, session_id):
  if line == "print schema list":        get_schema_list + get_current_schema
  if line 前缀 "select schema <id>":     select_schema
  if line 前缀 "select candidate <n>":   select_candidate_on_current_page(n-1) → print
  if line == "print candidate list":     candidate_list_begin/next/end 迭代
  if line 前缀 "set option [!]<name>":   解析 '!' 取反 → set_option
  if line == "synchronize":              sync_user_data
  if line 前缀 "delete ...":             delete_candidate(_on_current_page)
  否则返回 false（交给按键序列处理）
```

注意 `select candidate <n>` 用的是 `index - 1`：用户输入的 1-based 编号要换成 0-based 索引传给 API。

#### 4.4.3 源码精读

整个命令分发器见 [tools/rime_api_console.cc:99-190](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L99-L190)，几个关键片段：

- **`print schema list`**（[101-116 行](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L101-L116)）：遍历 `RimeSchemaList`，每项打印 `name` 与 `schema_id`，并调用 `get_current_schema` 显示当前方案，最后 `free_schema_list`。
- **`select schema <id>`**（[117-125 行](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L117-L125)）：用 `strncmp` 匹配前缀，取后半段作为 `schema_id` 调 `select_schema`。
- **`select candidate <n>`**（[126-137 行](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L126-L137)）：`atoi` 解析编号，转成 `index-1` 调 `select_candidate_on_current_page`，成功后立刻 `print`。
- **`print candidate list`**（[138-152 行](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L138-L152)）：用迭代器 `candidate_list_begin` → `candidate_list_next`（循环）→ `candidate_list_end` 遍历**所有**候选（不限当前页）。
- **`set option [!]<name>`**（[153-165 行](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L153-L165)）：若选项名前缀 `!`，表示「关闭」，否则「开启」，再调 `set_option`。

对应 API 表项：方案相关 [src/rime_api.h:344-350](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L344-L350)、option 相关 [src/rime_api.h:331-334](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L331-L334)、候选迭代与选择 [src/rime_api.h:435-452](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L435-L452)、`sync_user_data` [src/rime_api.h:303](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L303)。

#### 4.4.4 代码实践

**目标**：搞清楚一条命令从字符串到 API 调用的解析路径，并准备好在运行时亲手试。

**步骤**：

1. 假设你在 console 里输入 `select schema cangjie5`，跟踪代码：`execute_special_command` 用 `strncmp` 匹配前缀 `"select schema "`（含末尾空格），把指针后移得到 `schema_id = "cangjie5"`，再调 `rime->select_schema(session_id, "cangjie5")`。
2. 假设你输入 `set option !ascii_mode`，跟踪代码：检测到首字符 `!`，于是 `is_on = False`、`option = "ascii_mode"`，调 `set_option(session_id, "ascii_mode", False)`，即关闭英文模式（回到中文）。
3. 假设你输入 `select candidate 2`，跟踪代码：`index = 2`，传 `2-1 = 1` 给 `select_candidate_on_current_page`，即选中当前页的第 2 个候选。

**预期结果**：你会确认 console 的命令解析非常朴素（字符串前缀匹配 + `atoi`），没有用复杂的 shell 词法分析，因此命令格式必须**精确**（例如 `select schema ` 后必须有一个空格）。

> **运行时验证**：上面 3 条命令的实际输出（是否真的切到仓颉、是否真的选中第 2 候选）**待本地验证**——取决于 `data/minimal` 里方案的编译结果与当前候选。本讲末尾的综合实践会带你亲自跑一遍。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `select candidate` 要传 `index - 1`，而 `delete on current page` 直接传用户输入的 `index`？

> **答案**：因为两个 API 的约定不同。`select_candidate_on_current_page` 接受 **0-based** 索引（见 [src/rime_api.h:445-446](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L445-L446)），而 console 的菜单显示是 1-based（`printf("%d. ...", i+1)`），所以选择时要 `index-1`。而 `delete_candidate_on_current_page` 这条命令的实现没做减法（见 [tools/rime_api_console.cc:169-178](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L169-L178)）——这是个值得注意的不一致点，实际删除的索引与显示编号可能错位，属于示例程序里的小瑕疵。

**练习 2**：`candidate_list_begin/next/end`（迭代器）和直接读 `RimeContext.menu.candidates` 有什么区别？

> **答案**：`menu.candidates` 只含**当前页**的候选（受 `page_size` 限制）；而 `candidate_list_*` 迭代器可以**跨页**遍历整个候选列表，`next` 内部会按需翻页拉取。所以「看当前页」用 context，「列全部候选」用迭代器。

---

### 4.5 通知回调：on_message

#### 4.5.1 概念说明

除了前端**主动**调 `get_*` 去拉取状态，引擎还会**主动**向前端「推」消息：方案加载完成、某个开关变了、部署开始/成功/失败……这些事件通过 `set_notification_handler` 注册的回调函数异步送达。

消息用两个字符串描述：`message_type`（如 `"schema"`、`"option"`、`"deploy"`）和 `message_value`（如 `"luna_pinyin/Luna Pinyin"`、`"ascii_mode"`、`"!ascii_mode"`、`"success"`）。这套约定写在 [src/rime_api.h:216-234](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L216-L234) 的注释里，值得完整读一遍。

#### 4.5.2 核心流程

```text
引擎事件发生
  └── 调用 on_message(context_object, session_id, type, value)
        ├── printf("message: [...] [type] value")
        └── if RIME_API_AVAILABLE(rime, get_state_label) 且 type == "option":
              解析 value 前缀 '!' 得到 开/关 状态与选项名
              rime->get_state_label(...) 取人类可读标签
              printf("updated option: name = state // label")
```

注意 `option` 类消息里，`value` 以 `!` 开头表示「关闭」，否则表示「开启」（与 4.4 里 `set option` 命令的 `!` 约定一致）。

#### 4.5.3 源码精读

注册回调发生在 `main` 开头：[tools/rime_api_console.cc:227](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L227) 调 `rime->set_notification_handler(&on_message, NULL)`。

回调实现见 [tools/rime_api_console.cc:192-209](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc#L192-L209)，关键是用 `RIME_API_AVAILABLE(rime, get_state_label)` 先探测这个**较新**的函数指针是否存在（老版本 librime 没有它），存在才调用——这正是 u1-l4 讲过的 `RIME_API_AVAILABLE` 版本兼容机制的实战用法。

对应的宏定义见 [src/rime_api.h:518-519](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L518-L519)，`get_state_label` 函数指针见 [src/rime_api.h:478-480](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L478-L480)。

#### 4.5.4 代码实践

**目标**：理解通知消息的 (type, value) 约定，学会预测 console 会打印什么。

**步骤**：

1. 读 [src/rime_api.h:216-230](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L216-L230) 的注释，列出三类消息（`schema` / `option` / `deploy`）各自的 value 取值。
2. 假设你执行 `select schema cangjie5` 成功，预测控制台会多出一行什么样的 `message:` 输出。
3. 假设你执行 `set option ascii_mode`（开启英文模式），预测 `on_message` 会怎么解析这条 option 消息。

**预期结果**：

- 切换方案后，应出现形如 `message: [<id>] [schema] cangjie5/<方案名>` 的行（部署阶段还可能伴随 `[deploy] start` / `[deploy] success`）。
- 开启 `ascii_mode` 后，`message_value` 为 `"ascii_mode"`（无 `!`），`state` 解析为 `True`；若关闭则 `value` 为 `"!ascii_mode"`，`state` 为 `False`。
- 以上具体文案**待本地验证**（依赖方案名翻译与 `get_state_label` 返回值）。

#### 4.5.5 小练习与答案

**练习 1**：`on_message` 里为什么要先 `RIME_API_AVAILABLE(rime, get_state_label)` 再调用，而不是直接 `rime->get_state_label(...)`？

> **答案**：`get_state_label` 是后加进 `RimeApi` 表的字段。老版本的 librime 编译出的表更短，该槽位不存在；若直接调用，等于访问越界内存。`RIME_API_AVAILABLE` 先用 `RIME_STRUCT_HAS_MEMBER` 判断这个槽位在当前 `data_size` 范围内、且指针非空，再安全调用。这是「自版本化方法表」的标准防御写法。

**练习 2**：通知回调里 `session_id == 0` 意味着什么？

> **答案**：`session_id == 0` 表示这条消息**不针对任何具体会话**，而是全局事件。典型例子是部署消息（`[deploy] start/success/failure`）——部署是引擎级操作，不属于某个输入会话，所以 console 的 `on_message` 里用 `[%zu]` 打印出来的就是 0。

---

## 5. 综合实践

把 5 个模块串起来，做一次端到端的「编译 → 运行 → 观察」任务。

**目标**：亲手跑通 `rime_api_console`，用拼音打出一个词，切到仓颉方案再切回，观察三种输出结构与通知消息的变化。

**操作步骤**：

1. **编译**（参照 u1-l2 的构建方式，在仓库根目录）：

   ```bash
   make    # 等价于 cmake 配置 + 构建，产物在 build/
   ```

   构建成功后，`tools/CMakeLists.txt` 会自动把 `data/minimal/` 下的 `default.yaml`、`luna_pinyin.*`、`cangjie5.*`、`essay.txt`、`symbols.yaml` 拷贝到可执行文件输出目录（`build/bin/`）。

2. **进入数据所在目录运行**（关键：console 没有显式设 `shared_data_dir`，所以要在数据文件所在目录启动，让相对路径能找到 `default.yaml`）：

   ```bash
   cd build/bin
   ./rime_api_console
   ```

   启动后会看到 `initializing...` →（首次运行会编译最小词典，可能稍慢）→ `ready.`。

3. **打字**：输入 `ni` 回车，观察输出。预期会看到形如：

   ```text
   message: [<id>] [schema] luna_pinyin/Luna Pinyin
   schema: luna_pinyin / Luna Pinyin
   status: composing
   ni|                    # preedit，| 是光标
   page: 1  (of size 5)
   1. [你] (ni)
   2.  你 (ni)
   ...
   ```

4. **选候选**：输入 `select candidate 1`，观察 `commit: 你` 的出现，以及 `status` 中 `composing` 标志消失。

5. **切方案**：先 `print schema list` 看有哪些方案，再 `select schema cangjie5` 切到仓颉，观察 `message: [...] [schema] cangjie5/...` 通知。

6. **切回并试开关**：`select schema luna_pinyin` 切回，然后 `set option ascii_mode`（开英文模式）观察 `message: [...] [option] ascii_mode` 与 `updated option: ascii_mode = 1 // ...`。

7. **退出**：输入 `exit`。

**需要观察的现象**：

- 三种输出结构 `commit` / `status` / `context` 在「输入中 / 已提交 / 切方案」三个阶段的差异。
- `message:` 通知行的出现时机（切方案、改开关、部署）。
- 首次启动的 `initializing...` 到 `ready.` 之间发生的部署（如果数据已预编译，这段会很快）。

**预期结果**：你将完整看到「按键 → preedit/候选（Context）→ 选词 → 提交文字（Commit）」的整条链路在终端里呈现，并理解每一步对应 `RimeApi` 的哪个函数。

> **说明**：上述具体输出文案（候选文本、方案中文名、状态标签）依赖本地编译出的词典与方案配置，**待本地验证**。如果你在最小数据集下运行，候选数量与文本可能与安装完整 RIME 时不同，这是正常的。若运行时报找不到 `default.yaml`，请确认确实在 `build/bin/` 目录下执行（因为 console 未设 `shared_data_dir`，资源按相对路径解析）。

## 6. 本讲小结

- `rime_api_console.cc` 是一个**最小但完整的前端**，把 `RimeApi` 方法表里最常用的槽位全部用到了，是学习 C API 的最佳样例。
- 任何前端都要遵循固定生命周期：**setup → set_notification_handler → initialize → start_maintenance(+join) → 会话循环 → destroy_session → finalize**。
- 主循环用 `find_session` + `create_session` 实现**会话自愈**，用 `simulate_key_sequence` 把一行文本当成一串按键喂给引擎。
- 三种输出结构分工明确：`RimeCommit`（提交文字）、`RimeStatus`（模式标志）、`RimeContext`（preedit + 候选菜单），且 `get_*` 必须与 `free_*` **成对调用**，否则内存泄漏。
- console 用文本命令暴露了方案切换、候选选择、开关设置、候选迭代、用户数据同步等**会话级交互 API**，是理解真实前端如何操作输入法的钥匙。
- 通知回调 `on_message` 演示了 `RIME_API_AVAILABLE` 的版本兼容用法，以及「推」式事件（schema/option/deploy）的 `(type, value)` 约定。

## 7. 下一步学习建议

到这里，入门层（u1）就结束了——你已经能**用起来** librime。从 u2 开始，我们转入**运行时对象**的深入：

- **u2-l1 KeyEvent 与 KeyTable**：本讲的 `simulate_key_sequence` 内部其实是在拼装 `KeyEvent`。下一讲我们就拆开这个「按键的内部表示」，看 keycode + modifier 双字段模型，理解 `process_key(session_id, keycode, mask)` 的真正参数。
- **u2-l2 Service 与 Session**：本讲反复出现的 `create_session` / `find_session` 背后是 `Service` 单例。下一讲讲它如何管理多个会话、做过期清理。
- **u2-l3 Schema**：本讲用 `select_schema` 切换的「方案」到底是什么？下一讲拆开 `Schema = schema_id + Config`。
- **u2-l4 Engine**：`RimeContext` 里那些 preedit、候选是怎么算出来的？答案在 `Engine`，它是下一讲的主角，也是 u3「输入状态与候选生成」的入口。

建议阅读顺序：u2-l1 → u2-l2 → u2-l3 → u2-l4 → u3-l1。
