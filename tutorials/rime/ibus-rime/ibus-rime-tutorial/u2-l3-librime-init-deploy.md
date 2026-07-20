# librime 初始化、部署与通知

## 1. 本讲目标

本讲接着 [u2-l2](u2-l2-ibus-bus-factory.md) 停下的地方往下走。上一讲我们把 ibus-rime 接入了 IBus 总线、建好了工厂、注册了引擎、认领了总线名 `im.rime.Rime`。但此时 **librime 核心引擎还没有真正“跑起来”**：它的数据目录还没确定、方案还没部署、桌面上也不会有任何提示。

学完本讲，你将能够：

- 说清 `RimeTraits` 这个“特征结构”里装了哪些信息，以及为什么要装这些信息。
- 解释 librime 的 `setup` / `initialize` / `start_maintenance` 三步为什么不能合并成一步。
- 描述 `~/.config/ibus/rime` 用户目录是在什么时候、用什么权限被创建出来的。
- 理解 `deploy_config_file("ibus_rime.yaml", "config_version")` 背后的“版本戳”部署机制。
- 说清 `notification_handler` 是怎么把 librime 后台维护线程的 `deploy` 消息翻译成桌面通知的，以及为什么部署成功后要重新加载一次设置。

本讲只读 `rime_main.c` 一个文件，但它是 ibus-rime 与 librime 交互最密集的一段代码。

## 2. 前置知识

在进入源码前，先用大白话建立三个直觉。

### 2.1 “引擎”和“数据”是分开的

librime 是一个**算法库**，它本身不带词库和方案。方案（拼音表、笔顺表、英文方案……）和用户词库都存放在磁盘上的“数据目录”里。所以 librime 一启动，第一件事就是问：“我的数据放在哪？” 这就引出了两个目录：

- **shared data dir（共享数据目录）**：系统级、只读的预装方案，通常由 `rime-data` 包提供，在 ibus-rime 里由 CMake 变量 `RIME_DATA_DIR` 决定，编译期固化成宏 `IBUS_RIME_SHARED_DATA_DIR`。
- **user data dir（用户数据目录）**：每个用户私有、可写的目录，存放用户自定义方案、用户词库和用户配置。ibus-rime 把它放在 `~/.config/ibus/rime`。

### 2.2 “部署（deploy）”是什么

Rime 的方案源文件多为人类可读的 YAML，但运行时为了查词速度，librime 会把它们**编译**成二进制形式。这个“编译 + 校验 + 拷贝到用户目录”的过程，在 librime 里叫 **maintenance（维护）** 或 **deploy（部署）**。它可能比较耗时，所以 librime 把它放到**后台线程**里跑，跑的过程中通过回调发消息告诉前端进度。

### 2.3 前端怎么知道后台在干什么

因为维护是异步的，前端不能“等”它结束，只能“被通知”。librime 提供了一个 `set_notification_handler` 接口，让前端注册一个回调函数；后台线程在关键节点（开始、成功、失败）会调用它，传入 `message_type` 和 `message_value` 两个字符串。ibus-rime 在这个回调里调用 libnotify 弹出桌面通知。这就是本讲“通知”一节的来历。

> 名词速查：`RimeApi` 是 librime 暴露给前端的 **C API 结构体**（一组函数指针），在 `main()` 里通过 `rime_get_api()` 拿到，存进全局变量 `rime_api`。本讲里所有形如 `rime_api->xxx(...)` 的调用，都是在调 librime 的能力。

## 3. 本讲源码地图

本讲只涉及一个源文件，但它集中了 ibus-rime 与 librime 交互的全部代码：

| 文件 | 作用 |
| --- | --- |
| [`rime_main.c`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c) | 进程入口层。包含用户目录定位、特征填充、`ibus_rime_start/stop` 生命周期封装、`notification_handler` 与 `show_message` 通知、以及把它们串起来的 `rime_with_ibus()`。 |

辅助阅读（不在本讲精读范围，但会引用）：

| 文件 | 作用 |
| --- | --- |
| [`rime_config.h.in`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_config.h.in) | CMake 模板，生成 `IBUS_RIME_SHARED_DATA_DIR` 等宏。 |
| [`rime_settings.c`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c) | `ibus_rime_load_settings()` 的实现，部署成功后会被回调再次调用。 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块，按调用先后顺序排列。

### 4.1 RimeTraits 特征结构与 fill_traits 填充

#### 4.1.1 概念说明

librime 是一个会被很多前端（ibus-rime、fcitx-rime、Squirrel、Wayland 前端……）共用的核心库。它需要一种统一的方式问前端：“你是谁？你的数据放哪？你代表哪个发行版？” 这个“自我介绍”的载体，就是 **`RimeTraits`（特征结构）**。

可以把 `RimeTraits` 想象成一张“前端登记表”，主要字段包括：

- `shared_data_dir`：共享（只读）数据目录。
- `user_data_dir`：用户（可写）数据目录。
- `distribution_name`：发行版人类可读名（如 `Rime`）。
- `distribution_code_name`：发行版代号（如 `ibus-rime`）。
- `distribution_version`：版本号。
- `app_name`：应用标识（如 `rime.ibus`）。

librime 用 `distribution_*` 信息来给用户词库打“来源”标签、做兼容性判断；用 `app_name` 区分调用来源。

#### 4.1.2 核心流程

ibus-rime 把“填表”这件事抽成了一个独立函数 `fill_traits`，负责填那些**在编译期或进程期固定不变**的字段；而 `user_data_dir` 因为依赖运行时环境变量 `HOME`，放在调用点单独填。这样做的好处是：`setup` 和 `initialize` 两个阶段都要填表，复用同一个函数避免漏填。

```
fill_traits(traits):
    traits.shared_data_dir       = IBUS_RIME_SHARED_DATA_DIR   # 编译期宏
    traits.distribution_name     = "Rime"
    traits.distribution_code_name= "ibus-rime"
    traits.distribution_version  = IBUS_RIME_VERSION            # 编译期宏，如 1.6.1
    traits.app_name              = "rime.ibus"
```

#### 4.1.3 源码精读

常量定义（注意 `DISTRIBUTION_NAME` 用了 `_(x)` 宏，留作未来国际化的占位）：

[rime_main.c:L19-L21](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L19-L21) —— 定义发行版三件套，`DISTRIBUTION_VERSION` 直接取编译期生成的 `IBUS_RIME_VERSION` 宏。

`fill_traits` 本体：

[rime_main.c:L58-L64](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L58-L64) —— 把 5 个固定字段写进 traits。注意这里**没有**设置 `user_data_dir`，因为它要到运行时才知道。

而 `IBUS_RIME_SHARED_DATA_DIR` 的来源在模板文件里：

[rime_config.h.in:L7](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_config.h.in#L7) —— 该宏在 CMake 期被替换成 `@RIME_DATA_DIR@` 的实际值（详见 [u1-l2](u1-l2-build-and-run.md)）。

#### 4.1.4 代码实践

**目标**：确认 `fill_traits` 写入的值都来自哪里。

**步骤**：

1. 打开 `rime_main.c` 第 19–21 行，对照 `fill_traits`（第 58–64 行）。
2. 打开 `rime_config.h.in`，找到 `IBUS_RIME_VERSION` 和 `IBUS_RIME_SHARED_DATA_DIR` 两个 `@...@` 占位符。
3. 回想 [u1-l2](u1-l2-build-and-run.md)：`configure_file` 会把它们替换成 CMake 变量值。

**观察现象 / 预期结果**：你应该能说清——`distribution_name` 来自源码常量 `DISTRIBUTION_NAME`（`"Rime"`），`distribution_version` 来自构建系统注入的版本号，`shared_data_dir` 来自 CMake 探测到的 `RIME_DATA_DIR`。三者来源不同：源码、构建版本号、构建期目录探测。**（待本地验证：执行 `make` 后用 `grep` 看 `build/rime_config.h` 里这两个宏的实际值。）**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `user_data_dir` 不放进 `fill_traits`，而要在调用点单独赋值？
**答案**：因为 `user_data_dir` 依赖运行时环境变量 `HOME`（见 4.3 节），每次 `ibus_rime_start` 都可能需要重新计算；而 `fill_traits` 里的字段在编译期或进程期就固定了。分开填可以让固定字段复用、动态字段就近处理。

**练习 2**：如果某个前端想把 `app_name` 改成 `rime.fcitx`，librime 会受什么影响？
**答案**：`app_name` 主要用于 librime 内部区分调用来源与给用户数据打标记，改它不会破坏功能，但会改变用户词库等数据里记录的来源标识。

---

### 4.2 setup 与 initialize：librime 的两阶段初始化

#### 4.2.1 概念说明

librime 的启动**不是一步到位**的，它刻意分成了两个阶段：

- **`setup(traits)`**：登记“环境信息”——告诉 librime 数据目录在哪、发行版是谁。这一步**只是登记**，还不会真正创建引擎实例，也不会触发任何重活。它解决的是“你在哪个世界里”。
- **`initialize(traits)`**：真正初始化引擎实例、分配内部资源、准备处理会话。它解决的是“你现在可以干活了”。

为什么分开？因为 `setup` 之后、`initialize` 之前，前端可能还想做一些准备工作（比如先确保用户目录存在）。把它们拆开，就把“登记环境”和“启动引擎”解耦了。这种“先 setup 后 initialize”是 librime 对所有前端的统一约定。

此外，librime 的结构体用了一个叫 `RIME_STRUCT` 的宏来初始化。它的作用是把结构体清零，并在首字段里写入 librime 要求的大小/版本信息，用于**跨版本的 ABI 兼容**——这样即使未来 `RimeTraits` 增加字段，老前端和新库也能安全互操作。

#### 4.2.2 核心流程

`rime_with_ibus()` 里的两阶段调用：

```
# 第一阶段：登记环境（不含 user_data_dir）
RIME_STRUCT(RimeTraits, traits)   # 清零 + 填大小字段
fill_traits(&traits)              # 填固定字段
rime_api->setup(&traits)          # 登记给 librime

# 第二阶段：在 ibus_rime_start() 内部
RIME_STRUCT(RimeTraits, traits2)  # 再来一张表
fill_traits(&traits2)
traits2.user_data_dir = user_data_dir   # 这次带上用户目录
rime_api->initialize(&traits2)          # 真正初始化引擎
```

注意两个阶段用的是**两张不同的 traits 表**（都是栈上局部变量），各自走 `RIME_STRUCT` + `fill_traits`。

#### 4.2.3 源码精读

第一阶段在 `rime_with_ibus()` 里，紧接在 `set_notification_handler` 之后：

[rime_main.c:L121-L123](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L121-L123) —— `RIME_STRUCT` 声明并清零 `ibus_rime_traits`，`fill_traits` 填固定字段，`setup` 把环境登记给 librime。

第二阶段在 `ibus_rime_start()` 里：

[rime_main.c:L72-L76](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L72-L76) —— 重新 `RIME_STRUCT` + `fill_traits`，补上 `user_data_dir`，然后 `initialize`。

而 `ibus_rime_start()` 是被 `rime_with_ibus()` 在 setup 之后调用的：

[rime_main.c:L125-L127](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L125-L127) —— `full_check = FALSE`，调用 `ibus_rime_start`（内含 initialize），紧接着第一次 `ibus_rime_load_settings()`。

#### 4.2.4 代码实践

**目标**：把 `setup` 和 `initialize` 在代码里的位置画清楚。

**步骤**：

1. 在 `rime_main.c` 里定位 `rime_api->setup`（第 123 行）和 `rime_api->initialize`（第 76 行）。
2. 注意它们分别在两个不同函数里：`setup` 在 `rime_with_ibus`，`initialize` 在 `ibus_rime_start`。
3. 在第 126 行 `ibus_rime_start(full_check)` 处下断点或加日志，确认执行顺序。

**观察现象 / 预期结果**：执行顺序必须是 `setup`（第 123 行）→ `initialize`（第 76 行，经由 `ibus_rime_start`）。顺序反了会导致 librime 拿不到环境信息。**（源码阅读型实践，无需运行。）**

#### 4.2.5 小练习与答案

**练习 1**：如果把第 123 行的 `rime_api->setup(&ibus_rime_traits)` 删掉，只保留 `initialize`，会发生什么？
**答案**：librime 不会知道 shared_data_dir 和发行版信息，可能找不到预装方案、无法正确部署；行为不符合 librime 的前端约定。

**练习 2**：`RIME_STRUCT(RimeTraits, x)` 和直接写 `RimeTraits x = {0};` 的关键区别是什么？
**答案**：`RIME_STRUCT` 除了清零，还会在结构体首字段（librime 约定的大小/版本字段）写入 `sizeof(RimeTraits)`，用于 librime 做 ABI 版本兼容；直接 `{0}` 不会设置这个字段。

---

### 4.3 user_data_dir：用户数据目录的定位与创建

#### 4.3.1 概念说明

每个用户都有自己的输入习惯：自定义方案、用户词库、个性化配置。这些必须写到一个**用户私有且可写**的目录里。ibus-rime 选择的目录是 `~/.config/ibus/rime`，遵循 XDG Base Directory 规范的精神（用户配置放在 `~/.config/<应用>` 下）。

这个目录**不能假设它已经存在**——首次运行、或在新机器上，它很可能还没被创建。所以前端有责任在把它交给 librime 之前，先确保目录存在。这就是 `ibus_rime_start()` 开头那段“测试 + 建目录”代码的职责。

#### 4.3.2 核心流程

```
get_ibus_rime_user_data_dir(path):
    home = getenv("HOME")
    path = home + "/.config/ibus/rime"
    return path

ibus_rime_start():
    user_data_dir[512] = {0}
    get_ibus_rime_user_data_dir(user_data_dir)
    if 目录不存在:
        g_mkdir_with_parents(user_data_dir, 0700)   # 递归建目录，仅属主可读写执行
    # ... 然后把 user_data_dir 挂到 traits 上，调用 initialize
```

要点：

- 目录路径由 `HOME` 环境变量拼出来，**硬编码**了 `/config/ibus/rime` 后缀。
- 用 `g_file_test(..., G_FILE_TEST_IS_DIR)` 判断是否已经是目录。
- 用 `g_mkdir_withParents` 递归创建（父目录 `~/.config/ibus` 不存在也会一起建）。
- 权限 `0700` 表示**仅属主**有读/写/执行权限，保护用户词库隐私。

#### 4.3.3 源码精读

目录拼接函数：

[rime_main.c:L25-L30](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L25-L30) —— 用 `strcpy`/`strcat` 把 `$HOME` 和固定后缀拼起来。注意它依赖调用者传入的 `path` 缓冲区（这里固定 512 字节）。

目录的“按需创建”在 `ibus_rime_start` 开头：

[rime_main.c:L67-L71](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L67-L71) —— 先取目录、不存在就建。`0700` 是八进制权限位。

随后把目录挂到 traits 并交给 librime：

[rime_main.c:L74-L76](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L74-L76) —— `ibus_rime_traits.user_data_dir = user_data_dir` 后立刻调用 `initialize`。因为 `initialize` 是同步调用， librime 在返回前会把这些路径消费/拷贝走，所以指向栈缓冲区的指针在此处是安全的。

#### 4.3.4 代码实践

**目标**：观察用户目录的创建时机与权限。

**步骤**：

1. 删除（或重命名）你本机的 `~/.config/ibus/rime` 目录。
2. 启动 ibus-rime（`ibus restart` 或重新登录后由 IBus 拉起 `ibus-engine-rime --ibus`）。
3. 用 `ls -ld ~/.config/ibus/rime` 查看目录是否被重建、权限是什么。

**观察现象**：目录被重建。
**预期结果**：`drwx------`，即 `0700`，属主可读写执行，其他人无权限。**待本地验证（需要可运行的 Linux + IBus 环境）。**

> 改不了环境？退而求其次做源码阅读型实践：在第 70 行 `g_mkdir_with_parents` 处想象 `HOME=/tmp/fakehome`，手动推演 `path` 会拼成 `/tmp/fakehome/.config/ibus/rime`，并解释 `g_mkdir_with_parents` 为何能同时建出 `ibus` 这个中间父目录。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `0700` 而不是 `0755`？
**答案**：用户目录里存用户词库和个人配置，属于隐私数据，不应让同机其他用户读取；`0700` 只允许属主访问。

**练习 2**：如果环境变量 `HOME` 未设置，会怎样？
**答案**：`getenv("HOME")` 返回 `NULL`，随后 `strcpy(path, NULL)` 是未定义行为，可能崩溃。这是一个依赖 `HOME` 已被正确设置的隐含前提。

---

### 4.4 start_maintenance 与 deploy_config_file：部署流程

#### 4.4.1 概念说明

`initialize` 之后，引擎实例已经就绪，但用户的方案可能还没“编译”好。librime 提供 `start_maintenance(full_check)` 来触发一次维护：

- 它**异步**地在后台线程里检查并部署方案（编译 YAML、构建二进制词库等）。
- 返回值是 `Bool`：**是否启动了维护**。如果一切已是最新、无需维护，它可能返回假。
- 参数 `full_check` 为真时强制全量检查，为假时只做必要检查。ibus-rime 在正常启动时传 `FALSE`（`full_check = FALSE`）。

维护是异步的，所以前端不能假设它“已经完成”。但前端自己的配置文件 `ibus_rime.yaml` 需要在维护期间被部署（拷贝/刷新到用户目录），所以 ibus-rime 在 `start_maintenance` 返回真时，紧接着调用 `deploy_config_file`。

`deploy_config_file(file, version_key)` 的原理是一个**版本戳机制**：librime 读取配置里的某个版本字段（这里是 `config_version`），与已部署版本比较；如果不一致或尚未部署，就重新部署该配置文件。这样改了 `config_version` 就能强制刷新。

#### 4.4.2 核心流程

```
rime_api->initialize(&traits)                       # 引擎就绪
if rime_api->start_maintenance(full_check):         # 后台开始维护？
    rime_api->deploy_config_file(                   # 是 → 部署前端自己的配置
        "ibus_rime.yaml", "config_version")
```

维护期间，后台线程会通过 `notification_handler` 发出 `deploy`/`start`、`deploy`/`success`、`deploy`/`failure` 三类消息（见 4.5 节）。

用一个简单的关系式表达版本戳的判定逻辑（概念模型，非源码）：

\[
\text{需要部署} \;\iff\; \text{version}_{\text{源}} \neq \text{version}_{\text{已部署}}
\]

只要源文件里的 `config_version` 与用户目录里已部署的不一致，就触发一次部署。

#### 4.4.3 源码精读

`ibus_rime_start` 的维护与部署部分：

[rime_main.c:L76-L80](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L76-L80) —— 先 `initialize`，再用 `start_maintenance` 的返回值决定是否 `deploy_config_file`。注释 `// update frontend config` 点明这一步是“更新前端自己的配置”。

对照 `ibus_rime.yaml` 里的版本字段：

[ibus_rime.yaml:L3](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml#L3) —— `config_version: '1.0'`，正是 `deploy_config_file` 第二个参数 `"config_version"` 指向的字段名。

#### 4.4.4 代码实践

**目标**：理解 `start_maintenance` 返回值如何控制 `deploy_config_file`。

**步骤**：

1. 阅读 [rime_main.c:L77-L80](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L77-L80)，确认 `deploy_config_file` 是包在 `if (start_maintenance(...))` 里的。
2. 思考：如果 `start_maintenance` 返回假（无需维护），`ibus_rime.yaml` 还会被部署吗？

**观察现象 / 预期结果**：不会被部署——`deploy_config_file` 只在维护被启动时才执行。这意味着：如果 librime 认为一切最新，前端配置也不会被刷新。这正是版本戳机制“按需部署”的体现。**（源码阅读型实践。）**

#### 4.4.5 小练习与答案

**练习 1**：用户改了 `~/.config/ibus/rime/ibus_rime.yaml` 的样式但没改 `config_version`，重启 ibus-rime 后改动一定生效吗？
**答案**：不一定。如果 `start_maintenance` 返回假，`deploy_config_file` 不会被调用；不过 `ibus_rime_load_settings` 仍然会直接读取用户目录里的 yaml（见 4.5 节与 [u5-l1](u5-l1-yaml-config-loading.md)），样式改动通常仍能被读到。版本戳主要影响的是“是否重新部署/覆盖”这一步。

**练习 2**：为什么 ibus-rime 在启动时传 `full_check = FALSE` 而不是 `TRUE`？
**答案**：全量检查（`TRUE`）会强制重新编译所有方案，启动更慢；正常启动只需增量检查（`FALSE`），更快进入可用状态。全量检查通常留给用户主动点“部署”按钮时（见 [u3-l3](u3-l3-callbacks-and-toolbar.md)）。

---

### 4.5 libnotify 通知与 notification_handler

#### 4.5.1 概念说明

维护是异步跑在后台线程里的，用户需要知道“现在在部署 / 部署完成 / 部署失败”。librime 自己不会弹通知，它只通过 `set_notification_handler` 注册的回调发**消息字符串**。把消息变成**桌面通知**，是前端的职责——ibus-rime 用 **libnotify**（Linux 桌面通知的通用库，对接 freedesktop.org 的通知服务）来完成。

整个通知链路是：

```
librime 后台维护线程
   │  关键节点调用回调
   ▼
notification_handler(context, session_id, message_type, message_value)
   │  按 message_type/message_value 分支
   ▼
show_message(summary, details)
   │  创建 NotifyNotification 并 show
   ▼
桌面通知服务（弹出气泡）
```

#### 4.5.2 核心流程

`notification_handler` 只关心 `message_type == "deploy"` 一类消息，按 `message_value` 分三个分支：

| message_value | 动作 | 含义 |
| --- | --- | --- |
| `"start"` | `show_message("Rime is under maintenance ...", NULL)` | 维护开始，请稍候 |
| `"success"` | `show_message("Rime is ready.", NULL)` **+ `ibus_rime_load_settings()`** | 维护成功，配置已就绪，**重载设置** |
| `"failure"` | `show_message("Rime has encountered an error.", "See /tmp/rime.ibus.ERROR ...")` | 维护失败，指引自查错误日志 |

最值得注意的就是 **`success` 分支里的 `ibus_rime_load_settings()`**——这是本讲的核心问题，下面单独解释。

**为什么部署成功后要重新加载设置？**

1. 维护是**异步**的。第 127 行 `ibus_rime_load_settings()` 在 `ibus_main()` 之前调用，那时维护可能还在后台跑、`ibus_rime.yaml` 可能尚未被部署到最终状态。所以这第一次读取读到的是“启动时刻可得”的配置。
2. 当后台维护成功（`deploy`/`success`）时，`ibus_rime.yaml` 已经被刷新到用户目录、进入最终状态。此时**再读一次**，才能拿到被部署覆盖后的最新样式（如 `color_scheme`、`preedit_style` 等）。
3. 换句话说：第 127 行是“尽力先读一次，让引擎尽快可用”；第 48 行是“部署完成后补读一次，保证设置最新”。两次读取配合，兼顾了**启动速度**与**配置时效性**。

#### 4.5.3 源码精读

libnotify 的初始化与回调注册，在 `rime_with_ibus()` 里：

[rime_main.c:L115-L119](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L115-L119) —— `notify_init("ibus-rime")` 失败则 `exit(1)`（致命错误，因为无法向用户反馈部署状态）；随后 `set_notification_handler(notification_handler, NULL)` 把回调交给 librime，第二个参数 `NULL` 是会原样回传给回调 `context_object` 的上下文。

`show_message`：弹通知的最小封装：

[rime_main.c:L32-L36](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L32-L36) —— 新建 `NotifyNotification`、显示、`g_object_unref` 释放。图标参数传 `NULL`，使用系统默认图标。

`notification_handler` 本体：

[rime_main.c:L38-L56](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L38-L56) —— `deploy` 分支按 `start`/`success`/`failure` 三值分发。`success` 分支第 48 行调用 `ibus_rime_load_settings()`；末尾 `return` 保证只处理 `deploy` 类型，其他消息类型直接忽略。

`ibus_rime_load_settings()` 的实现（部署成功后会被它再次触发）：

[rime_settings.c:L42-L52](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L42-L52) —— 先把全局 `g_ibus_rime_settings` 重置为默认值，再 `config_open("ibus_rime", ...)` 打开配置，逐项读取。它的完整逻辑在 [u5-l1](u5-l1-yaml-config-loading.md) 详讲。

进程退出时对称地反初始化 libnotify：

[rime_main.c:L131-L132](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L131-L132) —— `ibus_main()` 返回后，`ibus_rime_stop()`（即 `rime_api->finalize()`）释放 librime，`notify_uninit()` 释放 libnotify。两者都与初始化成对。

#### 4.5.4 代码实践

**目标**：跟踪 `deploy`/`success` 分支，说清它为何调用 `ibus_rime_load_settings()`，并描述用户目录的创建时机。

**步骤**：

1. 从 [rime_main.c:L46-L49](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L46-L49) 进入 `success` 分支，确认它做了两件事：弹“Rime is ready.”通知、调用 `ibus_rime_load_settings()`。
2. 跳到 [rime_settings.c:L42-L52](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L42-L52)，确认该函数会重置默认值后**重新**读 `ibus_rime.yaml`。
3. 回看本讲 4.3 节 [rime_main.c:L67-L71](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L67-L71)，回答用户目录何时被创建。
4. 回看本讲 4.5.2 节的“两次读取”解释，把它用自己的话复述一遍。

**观察现象 / 预期结果**：

- **为何重载设置**：维护异步完成后，`ibus_rime.yaml` 已被刷新到最终状态，重读保证样式（如 `color_scheme`、`preedit_style`）取到部署后的最新值；而第 127 行的初次读取只是为了让引擎尽快进入可用状态。
- **用户目录创建时机**：`~/.config/ibus/rime` 在 `ibus_rime_start()` 的最开头（第 67–71 行）被创建，**早于** `initialize`、`start_maintenance` 和任何部署动作。也就是说：先有目录，引擎才被初始化，维护才可能往里写东西。

> 想亲眼看到通知？在桌面环境下运行 ibus-rime 并触发一次部署（点状态栏“部署”按钮，见 [u3-l3](u3-l3-callbacks-and-toolbar.md)），应能依次看到“Rime is under maintenance ...”和“Rime is ready.”两条气泡。**待本地验证。**

#### 4.5.5 小练习与答案

**练习 1**：`notification_handler` 的第 4 个参数 `message_value` 还可能有哪些值？目前代码对 `deploy` 以外的类型怎么处理？
**答案**：除 `deploy` 外，librime 还可能发其他类型的消息（如方案级提示），但 ibus-rime 当前只处理 `deploy`；其他类型进入函数后不匹配 `if (!strcmp(message_type, "deploy"))`，直接走到末尾 `return`，什么都不做。

**练习 2**：`notify_init` 失败时程序 `exit(1)`，这是否过于激进？
**答案**：从“输入法必须能给用户反馈部署状态”的角度，失去通知能力意味着用户无法感知部署失败，工程上选择致命退出是可理解的取舍；当然也可以设计成降级（仅写日志、继续运行），但当前实现选择了前者。

**练习 3**：为什么 `set_notification_handler` 的第二个参数传 `NULL`？
**答案**：ibus-rime 不需要在回调里拿到额外上下文（它直接操作全局 `g_ibus_rime_settings` 和全局 `rime_api`），所以传 `NULL`；这个值会原样作为回调首参 `context_object` 传回。

---

## 5. 综合实践

把本讲 5 个模块串起来，画一张 **librime 启动与部署时序图**，并配上**对应的源码行号**。要求：

1. 画出两条时间线：**主线程**（`rime_with_ibus` → `ibus_rime_start` → `ibus_main`）与 **librime 维护线程**。
2. 在主线程时间线上，按顺序标注以下事件并写出行号：
   - `notify_init` + `set_notification_handler`（[L115-L119](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L115-L119)）
   - `setup`（[L121-L123](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L121-L123)）
   - 建用户目录 `~/.config/ibus/rime`（[L67-L71](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L67-L71)）
   - `initialize`（[L76](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L76)）
   - `start_maintenance` + `deploy_config_file`（[L77-L80](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L77-L80)）
   - 首次 `ibus_rime_load_settings`（[L127](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L127)）
   - `ibus_main` 阻塞（[L129](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L129)）
3. 在维护线程时间线上，标注它向主线程回调发出的三条消息：`deploy`/`start`、`deploy`/`success`、`deploy`/`failure`，并指向 `notification_handler`（[L38-L56](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L38-L56)）。
4. 在 `success` 回调处，特别标出它触发 **第二次** `ibus_rime_load_settings`（[L48](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L48)），并用一句话解释为什么需要这第二次读取。

**验收标准**：图里能清楚看出“主线程先建目录再 initialize，维护线程异步跑，成功后回调主线程重载设置”这条主线，且每个事件都带正确的源码行号。

## 6. 本讲小结

- **特征结构 `RimeTraits`** 是前端向 librime 的“自我介绍表”，`fill_traits` 填固定字段（共享目录、发行版信息），`user_data_dir` 留到运行时单独填。
- librime 启动是**两阶段**的：先 `setup` 登记“你在哪个世界”，再 `initialize` 让引擎“可以干活”；ibus-rime 把 `initialize` 包进了 `ibus_rime_start()`。
- **用户目录** `~/.config/ibus/rime` 在 `ibus_rime_start` 开头被“按需创建”，权限 `0700`，时机早于 `initialize` 和任何部署。
- **`start_maintenance` 是异步**的，返回是否启动了维护；为真时才 `deploy_config_file("ibus_rime.yaml", "config_version")`，用版本戳决定是否刷新。
- **通知链路**：librime 后台线程 → `notification_handler`（按 `deploy`/`start`/`success`/`failure` 分支）→ `show_message` → libnotify 桌面气泡。
- **部署成功后重载设置**：因为维护异步完成后 `ibus_rime.yaml` 才进入最终状态，所以 `success` 分支调用 `ibus_rime_load_settings()` 补读一次，与启动时第 127 行的首次读取互补。

## 7. 下一步学习建议

本讲把 `rime_main.c` 的全部“ librime 接入”逻辑讲完了。从下一讲开始，视角从**进程层**转向**引擎对象层**：

- [u3-l1 引擎类型与生命周期](u3-l1-engine-type-lifecycle.md)：进入 `rime_engine.c`，看 `IBusRimeEngine` 这个 GObject 子类是怎么定义、初始化和销毁的，以及它持有哪些成员（session/status/table/props）。
- [u3-l2 会话与按键主链路](u3-l2-session-and-key-event.md)：看一次按键如何被转发给 librime 的 `process_key_event`，呼应本讲的“会话”概念。
- 想提前理解 `ibus_rime_load_settings()` 的完整细节，可以先跳读 [u5-l1 ibus_rime.yaml 与运行时配置加载](u5-l1-yaml-config-loading.md)，再回来看本讲的 4.5 节，会有更深的体会。
