# C API 入口 rime_api.h

## 1. 本讲目标

上一篇（u1-l3）我们建立了 librime 的目录地图，知道了「源码本体在 `src/`，对外只暴露少量公共头」。本讲我们就来打开这扇对外的「大门」——C API。

学完本讲，你应当能够：

1. 说清楚为什么 librime 的所有 C API 都从一个叫 `rime_get_api()` 的函数「总入口」进入，而不是导出一堆独立符号。
2. 看懂 `RimeApi` 这张巨大的「方法指针表」，并按 setup / session / input / config 等功能组找到需要的函数。
3. 理解 `RIME_STRUCT_INIT` / `RIME_STRUCT_HAS_MEMBER` 这套「自版本化结构体」机制如何在库升级时保持二进制兼容。

本讲只讲「怎么进门、表怎么读」，不展开具体行为（Session 生命周期留到 u2-l2，按键流水线留到 u6）。

## 2. 前置知识

读本讲前，最好先有这几个概念：

- **C 函数指针**：一个指向函数的指针，例如 `void (*fn)(int)`，可以像函数一样被「调用」。librime 的 API 表里装的就是这种指针。
- **结构体（struct）与 C ABI**：C 结构体在内存里的布局（字段顺序、对齐）构成「应用二进制接口」（ABI）。跨动态库传递结构体时，调用方和库必须对布局达成一致，否则会读到错位的字节。
- **二进制兼容性**：库升级后，老的可执行文件不想重新编译还能用，就要求新库保留旧的结构体布局。新增字段不能破坏老字段的位置。
- **opaque pointer（不透明指针）**：只暴露 `void*` 或前置声明的指针，把真实类型藏在库内部（如本讲的 `RimeConfig.ptr`）。调用方看不到内部结构，自然也不会因为内部改版而崩溃。

如果上面这些还不熟，没关系，本讲会结合 librime 的真实代码讲一遍。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/rime_api.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h) | 公共头文件。声明所有对外结构体（`RimeTraits`/`RimeApi`/`RimeCommit` 等）、版本兼容宏，以及唯一导出的入口函数 `rime_get_api`。 |
| [src/rime_api.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.cc) | 体积很小。只实现模块注册（`RimeRegisterModule`/`RimeFindModule`）、几个安全版路径 getter 和 `RimeGetVersion`。**注意：真正的方法实现不在这里**。 |
| [src/rime_api_impl.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h) | 所有 API 方法的**真正实现**都在这个 `.h` 里（`RimeInitialize`/`RimeProcessKey`/`rime_get_api` 等）。它被 `.cc` 文件「包含」两次，从而生成两种 API 风味。 |
| [src/rime_api_stdbool.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_stdbool.h) / [src/rime_api_stdbool.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_stdbool.cc) | C99 `bool` 风味的 API：把 `Bool`/`True`/`False` 改写成 `bool`/`true`/`false`，供偏好 C99 类型的前端使用。 |
| [src/rime/setup.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc) | `setup`/`initialize` 真正调用的 `SetupDeployer`、`LoadModules` 和默认模块列表 `kDefaultModules`/`kDeployerModules`。 |

> **一个容易踩坑的点**：很多人打开 `rime_api.cc` 想看 `RimeInitialize` 的实现，却发现文件只有 100 行出头、根本没有这些函数。这是因为 librime 用了一个巧妙的技巧——把所有实现写在 `rime_api_impl.h` 里，然后被两份 `.cc` 各 `#include` 一次（见 [src/rime_api_stdbool.cc:3](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_stdbool.cc#L3)），靠宏生成两套「风味」。读 API 实现时，请直接打开 `rime_api_impl.h`。

## 4. 核心概念与源码讲解

### 4.1 rime_get_api()：唯一的 API 总入口

#### 4.1.1 概念说明

设想最朴素的 C API 设计：把每个能力都导出成一个独立符号，前端直接 `RimeInitialize(...)`、`RimeProcessKey(...)` 调用。这种「扁平 API」librime 早期（v0.9）确实有过。

但它有个大麻烦：**版本兼容**。库一旦升级、增加了新函数，老的可执行文件里根本没有这些新符号的引用，没问题；可是当库**在同一个符号上修改了行为**，或前端想调用**新版本才有的函数**时，前端就必须重新编译。更糟的是，导出符号一多，符号表会非常臃肿。

librime v1.0+ 的解法是**只导出一个函数**：`rime_get_api()`。它返回一个指向「方法指针表」`RimeApi` 的指针。前端拿到这张表后，通过 `api->initialize(...)`、`api->process_key(...)` 来调用。这样：

- 对外只暴露一个稳定符号 `rime_get_api`，符号表干净。
- 库内部怎么改实现都行，只要表的布局向前兼容，前端不用重编。
- 前端可以在运行时「探测」某个方法是否存在（见 4.3），从而优雅地兼容新旧库。

#### 4.1.2 核心流程

`rime_get_api()` 内部用「懒初始化」（lazy init）的单例：

```
第一次调用 rime_get_api():
  1. 静态变量 s_api.data_size 为 0
  2. RIME_STRUCT_INIT 设置 data_size
  3. 逐个把函数指针填进 s_api（s_api.create_session = &RimeCreateSession ...）
  4. 返回 &s_api
后续调用:
  直接返回同一个 &s_api（地址不变）
```

#### 4.1.3 源码精读

入口声明只有一行，在头文件底部：

[src/rime_api.h:510-L514](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L510-L514) 声明 `rime_get_api` 返回一个「版本受控」的 `RimeApi*`。

真正的实现在 `rime_api_impl.h`：

[src/rime_api_impl.h:1132-L1137](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L1132-L1137) 静态局部变量 `s_api` 实现「调用 N 次返回同一指针」。

填充表的代码是一长串赋值，比如把 `initialize` 槽指向 `RimeInitialize`：

[src/rime_api_impl.h:1138-L1141](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L1138-L1141) 把 `setup`/`initialize`/`finalize` 等槽位逐个绑定到实现函数。

注意被绑定的这些 `RimeInitialize`、`RimeProcessKey` 等函数本身也被标注了 `RIME_DEPRECATED`（如 [src/rime_api_impl.h:51](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L51)）。在默认风味里 `RIME_DEPRECATED` 就是 `RIME_API`（见 [src/rime_api.h:37-L39](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L37-L39)），也就是说**它们仍被导出为旧的扁平符号**，但官方推荐通过 `rime_get_api()` 表来调用它们。

#### 4.1.4 代码实践

**目标**：验证 `rime_get_api()` 是一个单例。

**步骤**（源码阅读型，无需编译运行）：

1. 阅读 [src/rime_api_impl.h:1132-L1137](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L1132-L1137)。
2. 回答：`s_api` 是 `static` 局部变量，它的生命周期是什么？为什么 `if (!s_api.data_size)` 只在第一次进入时为真？

**预期结果**：`s_api` 是函数内 `static` 变量，只初始化一次、地址固定；`data_size` 在首次 `RIME_STRUCT_INIT` 后变为非 0，后续直接跳过赋值块。

**待本地验证**：如果你愿意，可以写一段最小 C 程序（链接 librime 后）：

```c
/* 示例代码：非项目原有 */
RimeApi* a = rime_get_api();
RimeApi* b = rime_get_api();
printf("%p == %p ? %d\n", (void*)a, (void*)b, a == b);  /* 期望打印 1（相等） */
```

#### 4.1.5 小练习与答案

**练习 1**：为什么 librime 不直接 `extern` 导出几百个 `RimeXxx` 函数，而要绕一圈用 `rime_get_api()`？

**参考答案**：为了版本兼容与符号卫生。只导出一个稳定符号，把全部能力收纳进一张可在运行时探测的「方法指针表」；库升级新增方法不破坏老前端，前端也能用 `RIME_API_AVAILABLE` 优雅地兼容新旧库。

**练习 2**：`rime_get_api()` 返回的指针，调用方需要 `free` 吗？

**参考答案**：不需要。它指向库内部的 `static` 变量 `s_api`，所有权属于库，调用方只是「借用」。

### 4.2 RimeApi：按功能分组的方法指针表

#### 4.2.1 概念说明

`RimeApi` 是一个「几乎只装函数指针」的结构体。它把 librime 的能力**按功能分组**排列，每组之间用注释隔开。读这张表，等于在读 librime 的「能力清单」。

需要强调：表里存的是**函数指针**，不是函数本身。`api->process_key(...)` 在运行时是「从表里取出指针、再调用」，多一次间接寻址，但换来了上面所说的灵活性。

#### 4.2.2 核心流程

可以把 `RimeApi` 想象成这样一张分组表：

| 功能组 | 代表方法 | 作用 |
| --- | --- | --- |
| setup | `setup` | 初始化**之前**的全局设置（路径、日志） |
| 入退 / 维护 | `initialize`/`finalize`/`start_maintenance` | 启动/关闭引擎、触发后台部署 |
| 部署 | `deploy`/`deploy_schema`/`sync_user_data` | 编译方案、同步用户数据 |
| **会话管理** | `create_session`/`find_session`/`destroy_session` | 创建/查询/销毁输入会话 |
| **按键处理（input）** | `process_key`/`commit_composition` | 把按键喂进会话 |
| 输出（output） | `get_commit`/`get_context`/`get_status` + 对应 `free_*` | 取出提交文本、上下文、状态 |
| 运行时选项 | `set_option`/`get_option`/`set_property` | 开关与属性（如 ascii_mode） |
| 方案 | `get_schema_list`/`select_schema` | 列出/切换输入方案 |
| **配置（config）** | `schema_open`/`config_open`/`config_get_string` | 读 YAML 配置 |
| 测试 | `simulate_key_sequence` | 用字符串模拟一连串按键 |
| 模块 | `register_module`/`find_module` | 注册/查找模块 |

注意「输出」组里每个 `get_*` 都配了一个 `free_*`：因为 C API 不自动管理内存，`get_context` 会 `new[]` 一堆 `char*`，调用方用完必须用 `free_context` 还回去。这是典型的「谁分配谁释放」的 C 内存契约。

#### 4.2.3 源码精读

整张表定义在这里：

[src/rime_api.h:259-L260](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L259-L260) `RimeApi` 结构体开头，先是 `data_size`，然后从 `setup` 开始排列。

各组的关键行号：

- setup 组：[src/rime_api.h:265](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L265) `void (*setup)(RimeTraits* traits);`
- 入退与维护：[src/rime_api.h:288-L293](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L288-L293) `initialize`/`finalize`/`start_maintenance`/...
- **会话管理**：[src/rime_api.h:307-L311](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L307-L311) `create_session`/`find_session`/`destroy_session`/`cleanup_stale_sessions`/`cleanup_all_sessions`
- **按键处理（input）**：[src/rime_api.h:315-L318](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L315-L318) `process_key`/`commit_composition`/`clear_composition`
- 输出（output）：[src/rime_api.h:322-L329](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L322-L329) `get_commit`/`free_commit`/`get_context`/`free_context`/`get_status`/`free_status`
- **配置（config）**：[src/rime_api.h:354-L370](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L354-L370) `schema_open`/`config_open`/`config_close`/`config_get_bool`/.../`config_end`

`process_key` 的签名尤其值得记一下：

[src/rime_api.h:315](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L315) `Bool (*process_key)(RimeSessionId session_id, int keycode, int mask);` —— 它不接收「按键对象」，而是接收原始的 `keycode`（键码）和 `mask`（修饰键位掩码）。这正是 u2-l1 要讲的 `KeyEvent` 双字段模型在 C 层的样子。

#### 4.2.4 代码实践

**目标**：本讲的主实践任务——在 `rime_api.h` 里定位「会话管理 / 按键处理 / 配置读取」三组方法，并写出从启动到打字的最小调用顺序。

**步骤**：

1. 打开 [src/rime_api.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h)。
2. 分别找到这三组（行号见 4.2.3），确认每组的注释分隔（如 `// session management`、`// input`、`// configuration`）。
3. 写出最小调用顺序清单（伪代码）：

```
api = rime_get_api()
RIME_STRUCT_INIT(RimeTraits, traits); traits.shared_data_dir = ...; traits.user_data_dir = ...;
api->setup(&traits)          # 配路径、日志
api->initialize(&traits)     # 加载模块、启动 Service
session_id = api->create_session()
api->process_key(session_id, keycode, mask)   # 逐键输入
# 之后用 get_commit / get_context / get_status 取结果
api->destroy_session(session_id)
api->finalize()
```

**需要观察的现象**：注意 `setup` 与 `initialize` 都接收同一个 `RimeTraits*`，但分工不同（4.4 会展开）；注意所有「会话相关」方法都带 `session_id` 参数，而 `setup`/`initialize`/`finalize` 不带。

**预期结果**：你应当能用行号回答「会话管理在 307–311、按键处理在 315–318、配置在 354–370」，并理解 `session_id` 是贯穿 input/output/options 组的主键。

#### 4.2.5 小练习与答案

**练习 1**：`get_context` 和 `free_context` 为什么总是成对出现？

**参考答案**：`get_context` 内部会用 `new[]` 分配若干 `char*`（preedit、候选文本等），这些内存属于调用方；不调 `free_context` 就会泄漏。C API 没有 RAII，必须靠「谁分配谁释放」的契约手动管理。

**练习 2**：`process_key` 用 `session_id` 定位会话，但 `setup`/`initialize` 没有 `session_id` 参数，为什么？

**参考答案**：`setup`/`initialize` 是**全局**初始化（配置 Service、加载模块），发生在任何会话创建之前；`session_id` 只有 `create_session` 之后才存在，所以 input/output/options 等会话级方法才需要它。

### 4.3 自版本化结构体：data_size 与版本兼容

#### 4.3.1 概念说明

这是 librime C API 最精巧的设计，也是本讲的难点。

问题场景：库升级了，给 `RimeStatus` 新增了一个字段 `is_ascii_punct`。可是老的前端程序里，`RimeStatus` 的结构体定义是旧的、没有这个字段。如果库往这个字段写数据，会**越界**写到调用方栈上未知的内存。

librime 的解法：**每个跨边界的结构体都以一个 `int data_size` 开头**，由**调用方**在传入前填好。`data_size` 记录的是「调用方编译时，这个结构体除了 `data_size` 本身之外还有多少字节」。库据此判断「调用方到底认识哪些字段」，从而只填写调用方知道的字段，安全地新增字段而不破坏老程序。

这就是「自版本化结构体」（self-versioned struct）：结构体自带一个「我是哪个版本/多大」的标记。

#### 4.3.2 核心流程

整套机制由三个宏配合完成。先看数学定义。

设某结构体类型 `T` 的总大小为 \(\text{sizeof}(T)\)，其首字段 `data_size` 是 `int`，大小为 \(\text{sizeof}(\text{int})\)。则：

\[
\text{data\_size} \;=\; \text{sizeof}(T) - \text{sizeof}(\text{int})
\]

也就是说，`data_size` = 「去掉首字段后的有效载荷字节数」。`RIME_STRUCT_INIT` 就是把 `data_size` 设成这个值：

[src/rime_api.h:60-L61](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L60-L61) `RIME_STRUCT_INIT(Type, var)`。

接下来是「判断某字段是否被调用方认识」。设字段 `member` 相对结构体首地址的偏移量为 \(\text{offset}(\text{member})\)，则「调用方的结构体总大小」等于：

\[
\text{sizeof}(\text{int}) + \text{data\_size}
\]

只要这个总大小严格大于字段偏移量，就说明该字段落在调用方已知范围内：

\[
\text{sizeof}(\text{int}) + \text{data\_size} \;>\; \text{offset}(\text{member})
\]

这正是 `RIME_STRUCT_HAS_MEMBER` 的实现（右侧用 `(char*)&member - (char*)&var` 算出偏移量）：

[src/rime_api.h:62-L64](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L62-L64) `RIME_STRUCT_HAS_MEMBER(var, member)`。

`RIME_STRUCT_CLEAR` 则在不清掉 `data_size` 的前提下，把后续载荷清零：

[src/rime_api.h:65-L66](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L65-L66) `memset` 从 `data_size` 之后开始清零。

库内部使用时，常见的是 `RIME_PROVIDED`——它额外要求字段非空，专用于「判断 traits 里某个指针字段是否被调用方填了」：

[src/rime_api.h:74-L75](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L74-L75) `RIME_PROVIDED(p, member)`。

为了方便，`RIME_STRUCT` 宏把「声明变量并清零 + `RIME_STRUCT_INIT`」合二为一：

[src/rime_api.h:69-L71](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L69-L71) `RIME_STRUCT(Type, var)`。

哪些结构体参与了这套机制？凡是带 `data_size` 字段的：

- `RimeTraits`：[src/rime_api.h:84-L86](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L84-L86)
- `RimeCommit`：[src/rime_api.h:146-L148](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L146-L148)
- `RimeContext`：[src/rime_api.h:155-L157](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L155-L157)
- `RimeStatus`：[src/rime_api.h:168-L170](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L168-L170)
- `RimeApi` 自身：[src/rime_api.h:259-L260](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L259-L260)

`RimeApi` 也有 `data_size`，所以前端能安全地「探测某个方法指针是否存在」——这就是 `RIME_API_AVAILABLE` 宏的用途：

[src/rime_api.h:518-L519](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L518-L519) `RIME_API_AVAILABLE(api, func)`：先判断「这个槽位在调用方的表里是否存在」，再判断「指针非空」。前端想用一个新版本才有的方法时，应当这样写：

```c
/* 示例代码：非项目原有 */
if (RIME_API_AVAILABLE(api, change_page)) {
    api->change_page(session_id, False);  /* 仅在新版库上调用 */
}
```

这套机制在真实代码里处处可见。例如 `RimeSetup` 在读 `traits` 的可选字段时：

[src/rime_api_impl.h:29-L32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L29-L32) 用 `RIME_PROVIDED` 与 `RIME_STRUCT_HAS_MEMBER` 判断是否该读取 `min_log_level`/`log_dir`（这两个是 v1.6 才加的字段，老调用方的 traits 里可能没有）。

#### 4.3.3 代码实践

**目标**：用 4.3.2 的公式手算一次「字段是否存在」。

**步骤**：

1. 假设某老前端在 v1.0 时代编译，其 `RimeTraits` 只到 `modules` 字段（[src/rime_api.h:101](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L101)），不含 v1.6 的 `min_log_level`。
2. 因此它 `RIME_STRUCT_INIT` 后的 `data_size` 较小。
3. 库内执行 `RIME_STRUCT_HAS_MEMBER(*traits, traits->min_log_level)` 时，代入公式 \(\text{sizeof}(\text{int}) + \text{data\_size} > \text{offset}(\text{min\_log\_level})\)，左侧（老 traits 的大小）会**小于**右侧（min_log_level 的偏移），结果为假。
4. 于是库跳过日志相关设置，不会越界。

**需要观察的现象**：正是因为这个判断，老前端即便链接到带 `min_log_level` 的新库，也不会因为库去写一个不存在的字段而崩溃。

**预期结果**：能口述「`data_size` 越小，表示调用方越老；库据此自我限制只写已知字段」。

#### 4.3.4 小练习与答案

**练习 1**：`RIME_STRUCT_INIT(Type, var)` 为什么是 `sizeof(Type) - sizeof(var.data_size)`，而不是直接 `sizeof(Type)`？

**参考答案**：因为 `data_size` 描述的是「**除首字段 data_size 之外**的载荷字节数」。如果存 `sizeof(Type)`，公式就会重复计入 `data_size` 自身，导致后续「总大小 = sizeof(int) + data_size」算错，`RIME_STRUCT_HAS_MEMBER` 的偏移比较就会失真。

**练习 2**：在「风味」机制里（见源码地图），`RimeApi` 与 `RimeApi_stdbool` 是两个不同的结构体类型，它们各自有自己的 `data_size`。这会不会破坏版本兼容？

**参考答案**：不会。两种风味的结构体布局**完全一致**（只是布尔类型在源码层的写法不同，`int` 与 `bool` 在这两种目标平台上尺寸/取值一致），`data_size` 含义相同，库内对 `data_size` 的判等逻辑照常工作。调用方选哪种风味，就调用对应风味的 `rime_get_api` / `rime_get_api_stdbool`。

### 4.4 RimeTraits 与 setup → initialize 的真实流程

#### 4.4.1 概念说明

光会读表还不够，本节把「调用顺序」落到真实代码：`setup → initialize → create_session → process_key` 这条主干，在 librime 内部究竟做了什么。串联这一切的配置载体，就是 `RimeTraits`。

`RimeTraits` 是一个自版本化结构体（首字段 `data_size`），装着「启动 librime 所需的全部环境信息」：数据目录、发行版信息、要加载的模块、日志级别等。

#### 4.4.2 核心流程

完整主干：

```
1. RIME_STRUCT(RimeTraits, traits)          # 声明并初始化 traits
   traits.shared_data_dir = ".../share/rime-data"
   traits.user_data_dir   = "~/.config/ibus/rime"
   traits.app_name        = "rime.myapp"

2. api->setup(&traits)
   ├─ rime_declare_module_dependencies()    # 静态库下显式链接 core/dict/gears/levers
   ├─ SetupDeployer(traits)                 # 把目录写进 Deployer 单例
   └─ SetupLogging(app_name, ...)           # 初始化 glog（若启用）

3. api->initialize(&traits)
   ├─ SetupDeployer(traits)                 # 再设一遍目录（幂等）
   ├─ LoadModules(kDefaultModules)          # 加载 "default" 模块组 = core+dict+gears
   └─ Service::instance().StartService()    # 启动 Service 单例

4. session_id = api->create_session()       # 由 Service 创建会话（含 Engine）

5. api->process_key(session_id, keycode, mask)
   └─ Service.GetSession(id)->ProcessKey(KeyEvent(keycode, mask))
```

注意 `setup` 与 `initialize` 的分工：`setup` 偏「全局环境」（日志、显式链接），`initialize` 才真正「拉起引擎」（加载模块、启动 Service）。两者都要 `RimeTraits`，但 `setup` 在前、只做一次准备。

#### 4.4.3 源码精读

`RimeTraits` 全貌：

[src/rime_api.h:84-L117](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L84-L117) 字段按版本分区注释（`// v0.9`、`// v1.6`），这正是 4.3 兼容机制所保护的对象。其中 `modules`（[src/rime_api.h:101](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L101)）允许前端指定「启动前要加载的模块列表」，为空则用默认组。

`setup` 的实现：

[src/rime_api_impl.h:25-L37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L25-L37) 依次声明依赖、设置 Deployer、（条件地）初始化日志。注意它用 `RIME_PROVIDED(traits, app_name)` 判断是否真的要建日志。

`initialize` 的实现：

[src/rime_api_impl.h:51-L56](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L51-L56) 这里能清楚看到「优先用 traits.modules，否则用 `kDefaultModules`」的二选一，最后 `StartService`。

默认模块组定义在 setup.cc：

[src/rime/setup.cc:36-L42](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L36-L42) `kDefaultModules` = `"default"` (+ 额外插件)，`kDeployerModules` = `"deployer"`。而「default / deployer」分别是模块组别名：

[src/rime/setup.cc:45-L46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L45-L46) `default = core + dict + gears`、`deployer = core + dict + levers`。这与 u1-l3 的小结完全对应。

`LoadModules` 的循环很简洁：

[src/rime/setup.cc:48-L55](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L48-L55) 遍历名字数组，找到模块就加载。

最后是按键落地：

[src/rime_api_impl.h:171-L178](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L171-L178) `RimeProcessKey` 从 `Service` 取出会话，构造 `KeyEvent(keycode, mask)` 交给会话处理。这就是 C 层 `keycode + mask` 进入 C++ 层 `KeyEvent` 的交接点（详见 u2-l1）。

#### 4.4.4 代码实践

**目标**：把 4.4.2 的主干「锚定」到具体行号，形成一张可复查的调用清单。

**步骤**：

1. 对照下表，逐行在源码里确认每一步对应的实现位置：

| 步骤 | C API 方法 | 实现位置 |
| --- | --- | --- |
| 准备环境 | `setup` | [src/rime_api_impl.h:25-L37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L25-L37) |
| 启动引擎 | `initialize` | [src/rime_api_impl.h:51-L56](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L51-L56) |
| 创建会话 | `create_session` | [src/rime_api_impl.h:149-L151](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L149-L151) |
| 输入按键 | `process_key` | [src/rime_api_impl.h:171-L178](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L171-L178) |

2. 在每个实现里找一句「最能代表这步职责」的代码（例如 `initialize` 里的 `Service::instance().StartService()`）。

**需要观察的现象**：`create_session` 和 `process_key` 都通过 `Service::instance().GetSession(...)` 拿到会话再操作——`Service` 是贯穿所有会话级操作的单例（u2-l2 详解）。

**预期结果**：你能不看本讲，仅凭 `rime_api.h` 与 `rime_api_impl.h` 复述出 `setup → initialize → create_session → process_key` 的实现落点。

#### 4.4.5 小练习与答案

**练习 1**：如果前端在 `RimeTraits` 里把 `modules` 设成了一个自定义列表，`initialize` 还会加载默认模块组吗？

**参考答案**：不会。`initialize` 用 `RIME_PROVIDED(traits, modules) ? traits->modules : kDefaultModules` 二选一（[src/rime_api_impl.h:53-L54](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L53-L54)）。指定了自定义列表就用自定义的，不再追加默认组——所以自定义列表里必须自行包含所需的基础模块。

**练习 2**：`setup` 里有一行 `rime_declare_module_dependencies();`。在动态库构建下它什么都不做（见 [src/rime_api.cc:22-L23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.cc#L22-L23)），在静态库构建下才会显式调用 `rime_require_module_core()` 等。为什么静态库需要这一步？

**参考答案**：模块靠「构造器」（`__attribute__((constructor))`，见 `RIME_REGISTER_MODULE` 宏）自动注册，但构造器只在**该翻译单元被链接进来**时执行。动态库里这些模块代码天然在库里；静态库下，若没有地方显式引用这些符号，链接器可能丢弃对应的 `.o`，导致模块注册构造器不执行。`rime_declare_module_dependencies()` 通过显式调用 `rime_require_module_*`「钉住」这些符号，保证模块被注册。

## 5. 综合实践

把本讲四节串起来，完成一个**只读源码、不需运行**的小任务：为前端开发者写一份「librime C API 启动到打字」的最小骨架注释文档。

要求：

1. 从 [src/rime_api.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h) 中挑选出实现下面骨架所需的**最少**方法指针，并标注它们所在的**功能组与行号**。
2. 为每个用到 `data_size` 的结构体标注「为什么必须先 `RIME_STRUCT_INIT`」。
3. 用 `RIME_API_AVAILABLE` 给一个「安全调用 `select_candidate`」的示例（假设你不确定目标库是否有这个方法）。

骨架（你来填注释与行号）：

```c
/* 示例代码：非项目原有 */
RimeApi* api = rime_get_api();

RIME_STRUCT(RimeTraits, traits);   /* 为什么不能省？ */
traits.shared_data_dir = "/usr/share/rime-data";
traits.user_data_dir   = "/home/me/.config/rime";
traits.app_name        = "rime.myapp";

api->setup(&traits);               /* 做了哪三件事？ */
api->initialize(&traits);          /* 与 setup 的区别？ */

RimeSessionId sid = api->create_session();

/* 用 simulate_key_sequence 一次输入多个键（testing 组） */
api->simulate_key_sequence(sid, "ni hao");

RIME_STRUCT(RimeCommit, commit);   /* 为什么不能省？ */
if (api->get_commit(sid, &commit)) {
    /* commit.text 是上屏文本 */
    api->free_commit(&commit);     /* 漏掉会怎样？ */
}

api->destroy_session(sid);
api->finalize();
```

完成后，你应当能用一句话回答：**「librime 的 C API 通过 `rime_get_api()` 返回一张自版本化的方法指针表，前端按 setup→initialize→session→input 的顺序驱动它。」**

## 6. 本讲小结

- librime 只导出一个符号 `rime_get_api()`，返回静态单例 `RimeApi` 指针；所有能力都是这张「方法指针表」里的槽位。
- `RimeApi` 按功能分组：setup / 入退维护 / 部署 / 会话管理 / input / output / options / 方案 / config / 测试 / 模块。会话级方法都以 `session_id` 为主键。
- `get_*` 与 `free_*` 成对出现，体现 C API 「谁分配谁释放」的内存契约。
- 「自版本化结构体」靠首字段 `data_size`：调用方填入 `sizeof(T) - sizeof(int)`，库用 `RIME_STRUCT_HAS_MEMBER`/`RIME_PROVIDED` 判断字段是否存在，从而安全地新增字段而不破坏老前端。
- `RIME_API_AVAILABLE(api, func)` 把同样的兼容思路用在「方法指针」上，让前端能探测新版本才有的方法。
- `setup` 偏全局环境（日志、显式链接），`initialize` 才加载模块并 `StartService`；`process_key` 在 C++ 层被包装成 `KeyEvent(keycode, mask)` 交给会话。

## 7. 下一步学习建议

- 下一篇 **u1-l5「实战：用 rime_api_console 体验输入流程」** 会把这些 API 真正跑起来，端到端走一遍 `setup → initialize → maintenance → session → process_key → get_context`，强烈建议接着读 [tools/rime_api_console.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_api_console.cc)。
- 进入 u2 后，**u2-l1** 会展开本讲反复出现的 `KeyEvent(keycode, mask)`，讲清键码与修饰键掩码的内部表示。
- **u2-l2** 会深入本讲所有会话级方法背后的 `Service` 单例与 `Session` 生命周期。
- 想了解 `LoadModules` 加载的具体内容，可先扫一眼 [src/rime/core_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc) 与 [src/rime/gear/gears_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/gears_module.cc)，这会在 **u5-l3 模块机制** 中系统讲解。
