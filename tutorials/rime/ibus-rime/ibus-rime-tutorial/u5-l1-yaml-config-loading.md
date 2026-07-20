# ibus_rime.yaml 与运行时配置加载

## 1. 本讲目标

在前面的 U3、U4 里，我们反复看到一个全局变量 `g_ibus_rime_settings`：候选表横排还是竖排、预编辑文本内联还是弹窗、光标停在插入点还是选中片段起点、选中片段要不要上色……这些渲染行为最终都追溯到它。本讲就回答一个上游问题：

> 这些设置是**从哪里来**的？谁读的？读到哪里去了？

学完本讲，你应当能够：

1. 说清 librime 的 `RimeConfig` 读取 API（`config_open` / `config_get_bool` / `config_get_cstring` / `config_close`）各自的作用与调用顺序。
2. 把 `ibus_rime.yaml` 里的每一个 `style` 选项对应到 `IBusRimeSettings` 结构体的某个字段。
3. 理解「内置默认值 + 配置覆盖」这一套设计，以及颜色方案（aqua/azure/ink/luna）的查表机制。
4. 动手新增一个 `style` 选项，并打通「yaml → 解析 → 全局结构 → 引擎消费」的完整链路。

## 2. 前置知识

阅读本讲前，建议你已经具备以下认知（这些在前面讲义中已建立）：

- **薄前端与 RimeApi 边界**（u1-l1、u2-l1）：ibus-rime 不含算法，通过全局指针 `rime_api`（类型 `RimeApi*`）调用 librime 暴露的 C API。本讲用到的 `config_open` 等函数都是 `rime_api` 上的**函数指针**。
- **librime 的两层数据目录**（u2-l3）：只读的共享数据目录（编译期宏 `IBUS_RIME_SHARED_DATA_DIR`）与可写的用户数据目录（`~/.config/ibus/rime`）。`ibus_rime.yaml` 就放在这两个目录里，librime 负责查找。
- **部署与维护**（u2-l3）：`deploy_config_file("ibus_rime.yaml", "config_version")` 会依据版本戳决定是否刷新配置；维护线程异步完成后，前端会**补读一次**配置。
- **设置如何被消费**（u4-l1 ~ u4-l3）：`g_ibus_rime_settings` 在 `ibus_rime_engine_update` 及候选表构建中被读取。本讲讲的是「写」这一侧。

如果几个术语还不熟，先记住一句话：**librime 帮我们读写 YAML，ibus-rime 只负责把读到的值塞进一个全局结构体**。

## 3. 本讲源码地图

本讲只涉及三个文件，非常集中：

| 文件 | 行数 | 作用 |
|------|------|------|
| [rime_settings.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c) | 92 | **设置层**全部实现：定义颜色方案表、默认值、`ibus_rime_load_settings()` 读取入口 |
| [rime_settings.h](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h) | 40 | 对外类型：`IBusRimeSettings` 结构体、两个枚举、颜色常量、`extern` 全局声明 |
| [ibus_rime.yaml](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml) | 29 | 用户可编辑的配置文件，`style` 下定义运行时样式 |

调用方（只看，不改）：`rime_main.c` 在启动与部署成功两处调用 `ibus_rime_load_settings()`；`rime_engine.c` 只读 `g_ibus_rime_settings`。

数据流向一句话概括：

```
ibus_rime.yaml  ──(librime config API)──▶  ibus_rime_load_settings()  ──▶  g_ibus_rime_settings  ──(只读)──▶  rime_engine.c
```

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：RimeConfig 配置读取 API、ibus_rime.yaml 默认样式、IBusRimeSettings 全局结构、颜色方案、默认值与覆盖。

### 4.1 RimeConfig 配置读取 API

#### 4.1.1 概念说明

librime 把「读写 YAML」这件事封装成了一组**配置句柄 API**，和操作文件句柄的思路很像：

- 先 `config_open` 打开一个具名配置，拿到一个 `RimeConfig` 句柄；
- 用 `config_get_bool` / `config_get_cstring` 等 getter 按「路径」取值；
- 取完 `config_close` 关闭句柄。

注意：**ibus-rime 自己不解析 YAML**。它只是 librime 的客户端。配置名 `"ibus_rime"`（不带扩展名）会被 librime 自动映射成 `ibus_rime.yaml` 文件，并在共享/用户数据目录里查找。这一点很关键——它解释了为什么我们改的是 `ibus_rime.yaml`，而代码里写的是字符串 `"ibus_rime"`。

#### 4.1.2 核心流程

读取一次配置的标准四步：

```
1. RimeConfig config = {0};
2. rime_api->config_open("ibus_rime", &config)   // 打开，失败返回 False
3. 循环：rime_api->config_get_bool / config_get_cstring(&config, "style/xxx", ...)
4. rime_api->config_close(&config)                // 必须关闭
```

「路径」用斜杠 `/` 表达 YAML 的嵌套层级，例如 `"style/inline_preedit"` 对应：

```yaml
style:
  inline_preedit: true
```

两个 getter 的返回约定不同，必须区分：

- `config_get_bool(&config, path, &out)` 返回 `Bool`：**是否成功取到**；真正的值写到出参 `out`。
- `config_get_cstring(&config, path)` 返回 `const char*`：取不到时返回 `NULL`，取到时返回** librime 内部拥有的字符串指针**，调用方**不能 `free`**，也不能在 `config_close` 之后继续使用。

#### 4.1.3 源码精读

打开配置、取不到就致命报错并直接返回，见 [rime_settings.c:47-51](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L47-L51)：

```c
RimeConfig config = {0};
if (!rime_api->config_open("ibus_rime", &config)) {
  g_error("error loading settings for ibus_rime");
  return;
}
```

> 中文说明：用配置名 `"ibus_rime"` 打开（librime 会找 `ibus_rime.yaml`）；打不开说明环境异常，用 `g_error` 记致命日志后直接 return（此时全局结构体仍是默认值，引擎不会崩）。

布尔型读取的代表——`inline_preedit`，见 [rime_settings.c:53-57](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L53-L57)：

```c
Bool inline_preedit = False;
if (rime_api->config_get_bool(
        &config, "style/inline_preedit", &inline_preedit)) {
  g_ibus_rime_settings.embed_preedit_text = !!inline_preedit;
}
```

> 中文说明：先把局部变量初始化为 `False`；`config_get_bool` 返回真表示「取到了」，才把值（用 `!!` 规范成 0/1）写入全局结构体。注意 `if` 判断的是「是否取到」，不是「值是否为真」——这是覆盖式读取的关键（见 4.5）。

字符串型读取的代表——`preedit_style`，见 [rime_settings.c:59-67](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L59-L67)：

```c
const char* preedit_style_str =
    rime_api->config_get_cstring(&config, "style/preedit_style");
if(preedit_style_str) {
  if(!strcmp(preedit_style_str, "composition")) {
    g_ibus_rime_settings.preedit_style = PREEDIT_STYLE_COMPOSITION;
  } else if(!strcmp(preedit_style_str, "preview")) {
    g_ibus_rime_settings.preedit_style = PREEDIT_STYLE_PREVIEW;
  }
}
```

> 中文说明：取到非空字符串后，用 `strcmp` 分发到枚举值；取不到（`NULL`）则什么都不做，保留默认值。`preedit_style_str` 是 librime 内部指针，只读比较、不释放。

最后是关闭句柄，见 [rime_settings.c:91](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L91)：

```c
rime_api->config_close(&config);
```

> 中文说明：读取完毕，关闭配置句柄，释放 librime 内部为这次读取分配的资源。此后前面那些 `config_get_cstring` 返回的指针就不再有效。

#### 4.1.4 代码实践

**目标**：亲手认清四个 API 的调用顺序与返回约定。

**步骤**：

1. 打开 [rime_settings.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c) 的 `ibus_rime_load_settings` 函数。
2. 用笔在 `config_open`、`config_get_bool`、`config_get_cstring`、`config_close` 四处画圈，并标上 1/2/3/4 的执行顺序。
3. 对每一处 `config_get_*`，回答两个问题：(a) 它判断的是「是否取到」还是「值的真假」？(b) 它的返回值/出参归谁所有？

**观察现象**：

- 你会发现 `config_get_bool` 的 `if` 包住的是「赋值动作」，即「取到才覆盖」。
- 你会发现全文没有任何 `g_free(preedit_style_str)` 或类似释放——因为字符串属于 librime。

**预期结果**：能口述「open → 多次 get → close」三段式，并解释为何字符串型 getter 不需要调用方释放。待本地验证：若你想确认 `config_get_cstring` 真的不归调用方所有，可在 `config_close` 之后打印该指针内容（仅作阅读实验，不要提交这种改动）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `config_close(&config)` 这一行删掉，程序还能正常工作吗？有什么隐患？
**答案**：功能上通常仍能工作（值已经写进全局结构体了），但会造成 librime 内部资源泄漏——每次 `ibus_rime_load_settings` 都会漏掉一次句柄资源。由于该函数在部署成功回调里会被再次调用，长期运行下泄漏会累积。

**练习 2**：`config_get_bool` 的第三个参数为什么是「指针」而不是直接返回值？
**答案**：因为返回值这个通道被用来表达「是否成功取到该键」（成功/失败两态），真正的布尔值只能通过出参指针回传。这与 C 标准库 `strtod` 用 `errno` 区分「失败」与「合法 0」是同一种设计思路。

---

### 4.2 ibus_rime.yaml 默认样式

#### 4.2.1 概念说明

`ibus_rime.yaml` 是**用户可见、可编辑**的配置文件。它在仓库里以「默认样例」存在，安装时被拷贝到数据目录（见 u1-l2 的 install 规则）；部署时，librime 会把共享目录的版本与用户目录的版本按 `config_version` 合并/刷新。

整个文件目前只有一个有意义的顶层块：`style`，下面挂着五个开关，分别控制候选表方向、预编辑内联、预编辑样式、光标类型、高亮配色。

#### 4.2.2 核心流程

文件结构与对应字段一览：

```yaml
config_version: '1.0'   # 版本戳，供 deploy_config_file 判断是否需要刷新

style:
  horizontal: true       # → lookup_table_orientation（横/竖排）
  inline_preedit: true   # → embed_preedit_text（预编辑是否内联）
  preedit_style: preview # → preedit_style 枚举（composition|preview）
  cursor_type: select    # → cursor_type 枚举（insert|select）
  color_scheme: ~        # → color_scheme 指针（null|aqua|azure|ink|luna）
```

**路径映射规则**：YAML 的嵌套用 `/` 拼成路径。`style` 下的 `color_scheme`，在代码里就是字符串 `"style/color_scheme"`。

**值类型映射**：

| YAML 写法 | C 端 getter | C 端落点 |
|-----------|------------|----------|
| `true` / `false` | `config_get_bool` | `gboolean` 字段 |
| `composition` / `preview` / `insert` / `select` 等字面量 | `config_get_cstring` + `strcmp` | 枚举字段 |
| `~`（YAML 的 null）或 `aqua` 等 | `config_get_cstring` | 颜色方案查表 |

#### 4.2.3 源码精读

`config_version` 是版本戳，见 [ibus_rime.yaml:3](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml#L3)：

```yaml
config_version: '1.0'
```

> 中文说明：这是 `deploy_config_file("ibus_rime.yaml", "config_version")`（见 u2-l3）用来判断「部署的副本是否需要刷新」的依据。改了 yaml 内容通常要同时 bump 这个版本戳，部署才会生效。

`style` 块整体见 [ibus_rime.yaml:5-29](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml#L5-L29)，其中方向开关见 [ibus_rime.yaml:6-7](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml#L6-L7)：

```yaml
style:
  # candidate list orientation (false|true).
  horizontal: true
```

> 中文说明：注释里写明取值是 `false|true`，`true` 表示候选词横排。该值最终驱动 `lookup_table_orientation`（见 4.3、4.5）。

颜色方案默认关闭，见 [ibus_rime.yaml:24-28](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml#L24-L28)：

```yaml
  # built-in color schemes ... (null|aqua|azure|ink|luna)
  # by default highlighting color is not used.
  color_scheme: ~
  # color_scheme: aqua
```

> 中文说明：`~` 是 YAML 的 null，等价于「不使用高亮配色」。注释列出五个合法取值，并给出切换示例（注释掉的 `aqua`）。

#### 4.2.4 代码实践

**目标**：建立「yaml 键 → 路径字符串 → C 字段」的映射表。

**步骤**：

1. 打开 [ibus_rime.yaml](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml)。
2. 对 `style` 下的五个键，分别写出：代码里查询它用的路径字符串（如 `"style/horizontal"`）、调用的 getter、写入的结构体字段。
3. 把仓库自带的默认值（`true`/`preview`/`select`/`~`）记下来——下一步会用到。

**观察现象**：你会注意到 yaml 里**注释掉**了 `composition` 和 `insert` 两行，说明它们是「备选写法示例」，并非生效值。

**预期结果**：得到一张 5 行的映射表。**注意**：仓库自带的 yaml 默认值（如 `preedit_style: preview`）与 4.5 将讲的 C 端硬编码默认值（`PREEDIT_STYLE_COMPOSITION`）**并不相同**——这正好是理解「默认值与覆盖」的切入点。

#### 4.2.5 小练习与答案

**练习 1**：如果用户把自己的 `ibus_rime.yaml` 里 `style` 这一整段删掉，会发生什么？
**答案**：`config_open` 仍能成功（文件还在，只是 `style` 键缺失），每个 `config_get_*` 都取不到值、返回「未取到」。于是所有字段保留 C 端硬编码默认值（见 4.5）。程序不会崩，只是表现退回内置默认。

**练习 2**：为什么路径写成 `"style/color_scheme"` 而不是 `"style.color_scheme"`？
**答案**：这是 librime `RimeConfig` API 的约定——用 `/` 作为层级分隔符。`.` 在 YAML 里本身可以出现在键名中（不是结构性符号），所以选 `/` 避免歧义。

---

### 4.3 IBusRimeSettings 全局结构

#### 4.3.1 概念说明

读出来的值需要一个落点。ibus-rime 用一个**全局单例结构体** `g_ibus_rime_settings` 来集中存放所有运行时样式设置。它被定义在 `rime_settings.c`，用 `extern` 在头文件里声明，于是整个程序（引擎层、设置层）都能读它。

这种「一个全局结构体 + 一次性加载」的写法，对于体量小、配置只在部署时变化的程序是非常合适的——简单、无锁、读取零成本。

#### 4.3.2 核心流程

结构体的生命周期：

```
程序启动
  └─ ibus_rime_load_settings()  ← 第一次加载（rime_main.c:127）
       └─ 写满 g_ibus_rime_settings
进入 ibus_main() 主循环
  └─ 部署完成回调
       └─ ibus_rime_load_settings()  ← 第二次加载（rime_main.c:48），刷新
引擎按键/渲染
  └─ 只读 g_ibus_rime_settings.*
```

字段总览：

| 字段 | 类型 | 含义 | 取值 |
|------|------|------|------|
| `embed_preedit_text` | `gboolean` | 预编辑文本是否内联到输入框 | TRUE/FALSE |
| `preedit_style` | `gint`（枚举） | 内联预编辑显示什么 | `PREEDIT_STYLE_COMPOSITION` / `PREEDIT_STYLE_PREVIEW` |
| `cursor_type` | `gint`（枚举） | 内联光标位置 | `CURSOR_TYPE_INSERT` / `CURSOR_TYPE_SELECT` |
| `lookup_table_orientation` | `gint` | 候选表方向 | `IBUS_ORIENTATION_SYSTEM/HORIZONTAL/VERTICAL` |
| `color_scheme` | `struct ColorSchemeDefinition*` | 高亮配色方案指针，可为 `NULL` | 见 4.4 |

#### 4.3.3 源码精读

结构体定义见 [rime_settings.h:27-33](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L27-L33)：

```c
struct IBusRimeSettings {
  gboolean embed_preedit_text;
  gint preedit_style;
  gint cursor_type;
  gint lookup_table_orientation;
  struct ColorSchemeDefinition* color_scheme;
};
```

> 中文说明：五个字段对应 yaml 的五个 `style` 选项。注意 `preedit_style`/`cursor_type`/`lookup_table_orientation` 都用 `gint` 而非枚举类型——这是 GObject/GLib 的常见习惯（枚举值当 int 存）。

两个枚举见 [rime_settings.h:11-19](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L11-L19)：

```c
enum PreeditStyle {
  PREEDIT_STYLE_COMPOSITION,
  PREEDIT_STYLE_PREVIEW,
};

enum CursorType {
  CURSOR_TYPE_INSERT,
  CURSOR_TYPE_SELECT,
};
```

> 中文说明：C 枚举从 0 开始。`PREEDIT_STYLE_COMPOSITION = 0`、`PREEDIT_STYLE_PREVIEW = 1`。这两个值会与 yaml 字符串经 `strcmp` 映射（见 4.1.3）。

全局声明与定义：头文件 `extern` 见 [rime_settings.h:35](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L35)，`.c` 文件里实际分配存储见 [rime_settings.c:24](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L24)：

```c
// rime_settings.h
extern struct IBusRimeSettings g_ibus_rime_settings;
// rime_settings.c
struct IBusRimeSettings g_ibus_rime_settings;
```

> 中文说明：典型的「头文件声明、源文件定义」单例模式。任何 `#include "rime_settings.h"` 的文件都能直接读 `g_ibus_rime_settings.xxx`，链接到同一份存储。

颜色常量（被引擎渲染时使用，但定义在设置头里）见 [rime_settings.h:7-9](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L7-L9)：

```c
#define RIME_COLOR_LIGHT  0xd4d4d4
#define RIME_COLOR_DARK   0x606060
#define RIME_COLOR_BLACK  0x000000
```

> 中文说明：三组固定灰/黑色，分别用于辅助文本高亮背景、候选词注释着灰、辅助文本选中片段前景。它们是编译期常量，**不**由 yaml 控制，与 4.4 的可配置颜色方案是两套东西。

#### 4.3.4 代码实践

**目标**：确认全局单例的「一处定义、多处读取」。

**步骤**：

1. 在 [rime_settings.h:35](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L35) 看到 `extern` 声明。
2. 在 [rime_settings.c:24](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L24) 看到定义。
3. 用编辑器全局搜索 `g_ibus_rime_settings.`，统计它在 `rime_engine.c` 里被读取了多少处（提示：候选表方向、预编辑分支、颜色高亮等多处）。

**观察现象**：读取点全部集中在 `rime_engine.c`，没有任何一处对它加锁——因为写入只发生在主线程的 `ibus_rime_load_settings`，读取也在主线程，天然无竞争。

**预期结果**：能在 `rime_engine.c` 找到约十余处 `g_ibus_rime_settings.xxx` 读取，例如 [rime_engine.c:326-327](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L326-L327) 决定走哪种预编辑分支、[rime_engine.c:345](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L345) 决定是否上色。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `color_scheme` 是**指针**，而其他字段是值？
**答案**：因为颜色方案是从一张静态表（4.4）里**选取一个已有条目**，存指针即可共享那份只读定义，无需拷贝 `text_color`/`back_color` 三个字段；同时指针可为 `NULL`，天然表达「不使用高亮」这一默认态。

**练习 2**：`g_ibus_rime_settings` 是零初始化（BSS 段）的。在 `ibus_rime_load_settings` 第一次被调用之前，它的 `embed_preedit_text` 是什么值？
**答案**：是 0（即 `FALSE`）。但这没有实际影响，因为 `ibus_rime_load_settings` 在 `ibus_main()` 之前就被调用了（见 [rime_main.c:127](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L127)），引擎进入主循环前全局结构体已经被填成正确值。

---

### 4.4 颜色方案（aqua/azure/ink/luna）

#### 4.4.1 概念说明

`color_scheme` 用来给「内联预编辑文本中需要聚焦的那一段」上前景色+背景色（详见 u4-l2 的选中片段高亮）。ibus-rime 内置了四套配色，写死在一张静态数组里，用一个 `select_color_scheme` 函数按名字查表。

这是一组典型的「**有限取值集合**」：合法名字只有 `aqua`/`azure`/`ink`/`luna` 四个，外加「不使用」（NULL）。yaml 里写别的名字不会报错，而是静默回退到 NULL。

#### 4.4.2 核心流程

查表流程：

```
config_get_cstring("style/color_scheme")  →  例如 "aqua"
        │
        ▼
select_color_scheme(settings, "aqua")
        │ 线性遍历 preset_color_schemes[]
        │ strcmp 匹配 color_scheme_id
        ├─ 命中 → settings->color_scheme = &该条目;  g_debug(...);  返回
        └─ 遍历完没命中 → settings->color_scheme = NULL  （回退）
```

每条方案是一个三元组 `{ 名字, 前景色, 背景色 }`，颜色用 24 位 RGB 整数（`0xRRGGBB`）。

#### 4.4.3 源码精读

颜色方案结构体见 [rime_settings.h:21-25](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L21-L25)：

```c
struct ColorSchemeDefinition {
  const char* color_scheme_id;
  int text_color;
  int back_color;
};
```

> 中文说明：`color_scheme_id` 是 yaml 里写的名字（如 `"aqua"`）；`text_color` 是选中片段文字颜色（前景），`back_color` 是背景色。

内置四套方案表见 [rime_settings.c:8-14](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L8-L14)：

```c
static struct ColorSchemeDefinition preset_color_schemes[] = {
  { "aqua",  0xffffff, 0x0a3dfa },
  { "azure", 0xffffff, 0x0a3dea },
  { "ink",   0xffffff, 0x000000 },
  { "luna",  0x000000, 0xffff7f },
  { NULL, 0, 0 }
};
```

> 中文说明：四套方案分别是 aqua（白字蓝底）、azure（白字蓝底，色值略异）、ink（白字黑底）、luna（黑字黄底）。最后一项 `{ NULL, 0, 0 }` 是**哨兵**（sentinel），标志数组结束——这样查表循环不需要预先知道长度。

查表函数见 [rime_settings.c:26-40](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L26-L40)：

```c
static void
select_color_scheme(struct IBusRimeSettings* settings,
                    const char* color_scheme_id)
{
  struct ColorSchemeDefinition* c;
  for (c = preset_color_schemes; c->color_scheme_id; ++c) {
    if (!strcmp(c->color_scheme_id, color_scheme_id)) {
      settings->color_scheme = c;
      g_debug("selected color scheme: %s", color_scheme_id);
      return;
    }
  }
  // fallback to default
  settings->color_scheme = NULL;
}
```

> 中文说明：循环条件 `c->color_scheme_id` 非空——遇到哨兵 `{NULL,...}` 即停。命中就把**数组元素的地址**赋给 `settings->color_scheme`，并打一条 `g_debug` 日志；遍历完没命中就置 `NULL`（回退到「不高亮」）。

调用点见 [rime_settings.c:85-89](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L85-L89)：

```c
const char* color_scheme =
  rime_api->config_get_cstring(&config, "style/color_scheme");
if (color_scheme) {
  select_color_scheme(&g_ibus_rime_settings, color_scheme);
}
```

> 中文说明：注意外层 `if (color_scheme)`——yaml 里写 `color_scheme: ~`（null）时，`config_get_cstring` 返回 `NULL`，于是**根本不调用** `select_color_scheme`，`color_scheme` 字段保持默认值 `NULL`（不高亮）。这是「显式 null = 关闭」的语义。

引擎消费侧——选中片段上色，见 [rime_engine.c:379-392](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L379-L392)（composition 分支）：

```c
if (has_highlighted_span && g_ibus_rime_settings.color_scheme) {
  ...
  ibus_attr_list_append(
      inline_text->attrs,
      ibus_attr_foreground_new(
          g_ibus_rime_settings.color_scheme->text_color, start, end));
  ibus_attr_list_append(
      inline_text->attrs,
      ibus_attr_background_new(
          g_ibus_rime_settings.color_scheme->back_color, start, end));
}
```

> 中文说明：引擎只在「有选中片段 `has_highlighted_span`」且「配色指针非空」时才上色，取出 `text_color`/`back_color` 作为 `IBusText` 的前景/背景属性。若 `color_scheme` 为 `NULL`，整段不进入，自然没有高亮——与 4.5 的默认值呼应。

#### 4.4.4 代码实践

**目标**：体验「新增一套内置配色」有多简单，并观察 `g_debug` 日志。

**步骤**：

1. 在 [rime_settings.c:8-14](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L8-L14) 的数组里、哨兵之前插入一行，例如：
   ```c
   { "mint", 0x003322, 0x66ffcc },
   ```
2. 重新编译（`make`，参考 u1-l2）。
3. 把数据目录里的 `ibus_rime.yaml` 改为 `color_scheme: mint`，触发一次部署。
4. 以调试方式运行（设置环境变量让 GLib 输出 `g_debug`，例如 `G_MESSAGES_DEBUG=all ibus-engine-rime --ibus`）。

**观察现象**：日志里应出现 `selected color scheme: mint`。

**预期结果**：候选/预编辑选中片段呈现你定义的配色。待本地验证：不同桌面环境下 IBus 面板对前景/背景属性的支持程度不同，若看不到效果，可先切到内置的 `ink`（白字黑底，最明显）确认链路通畅。

#### 4.4.5 小练习与答案

**练习 1**：为什么哨兵 `{ NULL, 0, 0 }` 是必要的？
**答案**：因为 `preset_color_schemes` 是 C 数组，本身不携带长度信息。查表循环用 `c->color_scheme_id` 是否非空作为终止条件，必须有哨兵才能在遍历完四条后停下来，否则会越界读内存。

**练习 2**：如果 yaml 里写 `color_scheme: Aqua`（大写 A），会怎样？
**答案**：不会命中——`strcmp` 区分大小写，遍历完没匹配，回退到 `NULL`，即不高亮。日志里也不会出现 `selected color scheme`。这是「静默回退」的行为，不报错。

---

### 4.5 默认值与覆盖（ibus_rime_load_settings 主流程）

#### 4.5.1 概念说明

把前四个模块串起来的就是 `ibus_rime_load_settings` 这个唯一入口。它体现了一个非常重要的设计模式：**内置默认值 + 配置按需覆盖**。

思路是：

1. 维护一个静态的「硬编码默认」结构体 `ibus_rime_settings_default`。
2. 每次加载，**先把整个全局结构体整体赋值成默认值**（相当于「复位」）。
3. 然后逐项尝试读 yaml：**读到才覆盖，读不到就保留默认**。

这样做有两个好处：第一，调用幂等——重复调用结果一致；第二，缺省安全——yaml 缺哪个键，哪个键就用合理的内置默认值，永远不会出现「未初始化」的字段。

#### 4.5.2 核心流程

```
ibus_rime_load_settings()
  │
  ├─① g_ibus_rime_settings = ibus_rime_settings_default;   // 整体复位到默认
  │
  ├─② config_open("ibus_rime", &config)                     // 打不开 → g_error + return
  │
  ├─③ 逐项「读到才覆盖」：
  │     ├─ inline_preedit   (get_bool)
  │     ├─ preedit_style    (get_cstring + strcmp)
  │     ├─ cursor_type      (get_cstring + strcmp)
  │     ├─ horizontal       (get_bool)
  │     └─ color_scheme     (get_cstring + select_color_scheme)
  │
  └─④ config_close(&config)
```

需要特别区分**两套默认值**，它们**并不相同**：

| 字段 | C 硬编码默认（无 yaml 时生效） | 仓库自带 yaml 默认（安装后生效） |
|------|------------------------------|----------------------------------|
| `embed_preedit_text` | `TRUE` | `true`（一致） |
| `preedit_style` | `PREEDIT_STYLE_COMPOSITION` | `preview`（**不同**） |
| `cursor_type` | `CURSOR_TYPE_INSERT` | `select`（**不同**） |
| `lookup_table_orientation` | `IBUS_ORIENTATION_SYSTEM` | 横排（由 `horizontal: true` 推出，**不同**） |
| `color_scheme` | `NULL` | `~`（一致，都为空） |

也就是说：仓库自带的 yaml 已经把若干默认「翻转」了（preview + select + 横排）；只有当用户**删掉**这些键、或 yaml 缺失时，C 硬编码默认才会接管。

#### 4.5.3 源码精读

硬编码默认结构体见 [rime_settings.c:16-22](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L16-L22)：

```c
static struct IBusRimeSettings ibus_rime_settings_default = {
  .embed_preedit_text = TRUE,
  .preedit_style = PREEDIT_STYLE_COMPOSITION,
  .cursor_type = CURSOR_TYPE_INSERT,
  .lookup_table_orientation = IBUS_ORIENTATION_SYSTEM,
  .color_scheme = NULL,
};
```

> 中文说明：用 C99 指定初始化器（`.field = value`）逐字段赋默认值。`color_scheme = NULL` 表示默认不上色。`IBUS_ORIENTATION_SYSTEM` 表示「跟随系统设置」。

整体复位——函数第一行见 [rime_settings.c:45](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L45)：

```c
g_ibus_rime_settings = ibus_rime_settings_default;
```

> 中文说明：这是结构体的**整体赋值**，C 会逐成员拷贝。注意它拷的是 `color_scheme` 指针本身（指向静态数组里的某条目或 NULL），不会深拷贝颜色定义——因为颜色定义是只读共享的。

方向选项的覆盖逻辑见 [rime_settings.c:79-83](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L79-L83)：

```c
Bool horizontal = False;
if (rime_api->config_get_bool(&config, "style/horizontal", &horizontal)) {
  g_ibus_rime_settings.lookup_table_orientation =
    horizontal ? IBUS_ORIENTATION_HORIZONTAL : IBUS_ORIENTATION_VERTICAL;
}
```

> 中文说明：注意这里把 yaml 的布尔 `horizontal` **翻译**成了 IBus 的方向枚举——`true` → `HORIZONTAL`，`false` → `VERTICAL`。只有读到值才覆盖；读不到则保留 ① 处复位的 `IBUS_ORIENTATION_SYSTEM`。这正是 u4-l3 候选表方向的最终来源。

「读到才覆盖」与「读到才上色」的对照见 [rime_settings.c:85-89](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L85-L89)：`color_scheme` 只有在 `config_get_cstring` 返回非空时才查表；返回 `NULL`（yaml 写 `~` 或缺键）则保持默认 `NULL`。

加载入口的两个调用点：启动时见 [rime_main.c:127](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L127)（进入 `ibus_main` 主循环前），部署成功回调里见 [rime_main.c:48](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L48)。后者是 u2-l3 提到的「补读」——因为维护线程可能刚把 `ibus_rime.yaml` 刷新到新版本，需要重新读一次保证时效。

#### 4.5.4 代码实践

**目标**：用「删键」验证 C 硬编码默认会接管。

**步骤**：

1. 备份你的 `~/.config/ibus/rime/ibus_rime.yaml`（或共享数据目录里的同名文件）。
2. 把 `preedit_style: preview` 这一行**整行删掉**（不是注释，是删除），保留其余内容。
3. 触发一次部署（重启 ibus-rime，或点击状态栏「部署」按钮——见 u3-l3，等价于 `stop + start(TRUE)`）。
4. 在一个文本框里输入拼音，观察内联预编辑显示的是「转换后汉字」还是「原始编码」。

**观察现象**：删除 `preedit_style` 后，由于读不到该键，全局结构体保留 ① 处复位的 C 默认 `PREEDIT_STYLE_COMPOSITION`，内联预编辑应显示**原始编码**（而非 preview 模式的转换后汉字）。

**预期结果**：行为与「`preedit_style: composition`」一致。这证明「yaml 缺键 → C 默认接管」的覆盖模型。待本地验证：若你把整段 `style` 都删掉，五个字段会全部退回 C 硬编码默认（横排变 SYSTEM、preview 变 COMPOSITION、select 变 INSERT、不高亮），可逐一对照上表确认。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `ibus_rime_load_settings` 开头要先做一次「整体复位」，而不是只「读到才覆盖」？
**答案**：为了保证幂等与一致性。该函数会被调用两次（启动 + 部署成功）。如果只覆盖不清零，那么「第一次 yaml 有 `color_scheme: aqua`、第二次 yaml 改成 `color_scheme: ~`」时，第二次读不到非空值就不覆盖，`color_scheme` 会错误地保留 `aqua`。先复位成默认（`NULL`）再覆盖，才能正确反映「用户后来关掉了高亮」。

**练习 2**：`ibus_rime_settings_default` 被声明为 `static`，外部文件看不到它。这会带来问题吗？
**答案**：不会。它只在 `rime_settings.c` 内部被 `ibus_rime_load_settings` 用来做复位模板，外部世界只需要通过 `g_ibus_rime_settings` 这个 `extern` 全局读取结果即可。`static` 反而避免了符号污染，是好习惯。

---

## 5. 综合实践

把本讲全部知识串起来，完成一个「**新增一个 style 选项并打通全链路**」的小扩展。

**任务**：新增一个布尔选项 `style/show_comment`，用来在候选表里控制是否显示候选词注释（默认显示）。

**建议步骤**：

1. **扩结构体**：在 [rime_settings.h:27-33](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L27-L33) 的 `IBusRimeSettings` 里加一个字段 `gboolean show_comment;`。

2. **设默认**：在 [rime_settings.c:16-22](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L16-L22) 的 `ibus_rime_settings_default` 里加 `.show_comment = TRUE,`（默认显示注释）。

3. **加读取**：在 `ibus_rime_load_settings`（[rime_settings.c:42-92](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L42-L92)）里，仿照 `inline_preedit` 的写法，加：
   ```c
   Bool show_comment = True;
   if (rime_api->config_get_bool(&config, "style/show_comment", &show_comment)) {
     g_ibus_rime_settings.show_comment = !!show_comment;
   }
   ```

4. **在引擎消费**：找到 u4-l3 讲过的候选表注释拼接处（`g_strconcat` 那一带，见 [rime_engine.c:464](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L464) 附近），用 `if (g_ibus_rime_settings.show_comment)` 包住注释拼接。

5. **写 yaml**：在 [ibus_rime.yaml](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml) 的 `style` 下加 `show_comment: false`，bump `config_version`，部署后观察候选表是否不再显示注释。

**验收**：

- 改成 `false` → 注释消失；
- 删掉该行 → 注释重新出现（说明 C 默认 `TRUE` 接管）。

这个练习完整走过「yaml 键 → 路径 → getter → 全局结构体字段 → 引擎消费」五个环节，是 u6-l2「二次开发」的预演。

## 6. 本讲小结

- ibus-rime **不自己解析 YAML**，而是通过 librime 的 `RimeConfig` API（`config_open` / `config_get_bool` / `config_get_cstring` / `config_close`）按「`style/xxx`」路径取值。
- `ibus_rime.yaml` 的 `style` 块有五个选项：`horizontal`、`inline_preedit`、`preedit_style`、`cursor_type`、`color_scheme`，分别落到全局结构体 `g_ibus_rime_settings` 的五个字段。
- `g_ibus_rime_settings` 是 `extern` 全局单例，定义在 `rime_settings.c`、声明在 `rime_settings.h`，引擎层只读复用；写入只发生在主线程，天然无锁。
- 颜色方案是一张带哨兵的静态表 `preset_color_schemes[]`（aqua/azure/ink/luna），`select_color_scheme` 线性查表、未命中回退 `NULL`。
- 核心模式是「**内置默认值 + 读到才覆盖**」：每次加载先整体复位到 `ibus_rime_settings_default`，再逐项按 yaml 覆盖；yaml 缺键则用 C 默认。该函数在启动与部署成功时各调用一次，幂等且兼顾配置时效。

## 7. 下一步学习建议

- **走向工程化**：本讲只动了「读配置」，可继续读 u6-l1，看 `ibus_rime.yaml` 是如何被打包脚本（`package/make-binpkg-static` 等）安装到数据目录、`FindRimeData.cmake` 又是如何定位该目录的。
- **走向二次开发**：综合实践里那个 `show_comment` 扩展就是 u6-l2「架构取舍与二次开发」的缩影。建议接着读 u6-l2，系统了解「新增配置项 / 状态栏按钮 / 按键处理」三类扩展点的完整改造路径。
- **回看消费侧**：如果对 `color_scheme` / `preedit_style` 如何影响渲染还想更透彻，可以带着本讲对全局结构体的理解，重读 u4-l2（预编辑与辅助文本）与 u4-l3（候选表）的源码精读段，体会「写」与「读」两端的对应关系。
