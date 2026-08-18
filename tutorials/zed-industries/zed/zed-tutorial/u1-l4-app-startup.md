# 应用启动流程:从 main 到窗口出现

## 1. 本讲目标

学完本讲,你应该能够:

1. 完整复述 Zed 从 `main()` 执行到第一个窗口出现之间的关键阶段,并说出每个阶段做了什么。
2. 理解三个「启动基石」是如何奠定的:平台选择(`gpui_platform::current_platform`)、应用对象构建(`Application`)、崩溃处理与全局分配器。
3. 定位命令行参数(`Args`)的定义与解析位置,知道 `--askpass`、`--crash-handler`、`--system-specs` 这类「旁路模式」会在窗口出现之前就返回。
4. 在 `app.run(...)` 闭包这个「初始化大本营」里,找到设置加载(`settings::init`)、主题注册(`theme_settings::init`)、扩展宿主(`extension_host::init`)、会话(`AppSession`)与工作区初始化(`initialize_workspace`)各自所在的行。
5. 通过自己的动手实验(加 `eprintln!` 日志)验证你对启动顺序的理解,而不是靠猜。

本讲只做「流程级」理解:每个被调用的 `init` 内部做了什么,留给后续单元;GPUI 的 `App`、`Entity` 等机制将在第二单元展开,本讲只需要把它们当成「应用上下文」和「带句柄的状态对象」即可。

## 2. 前置知识

在学习本讲之前,请确认你已经了解(来自 u1-l1 ~ u1-l3):

- Zed 是一个由 243 个产品 crate 组成的 Cargo workspace,裸 `cargo run` 只构建 `crates/zed` 这个主程序(default-members)。
- `crates/zed` 是整个编辑器的**二进制 crate**(可执行程序),其余大部分是库 crate。

本讲还会用到下面几个基础概念,先用一段话通俗解释:

- **`main()` 函数**:Rust 二进制 crate 的入口。`crates/zed/src/main.rs` 就是 Zed 的入口文件。
- **模块与 `mod`**:`main.rs` 开头有 `mod zed;`,表示「把 `zed.rs`(及其子目录 `zed/`)作为本 crate 的一个子模块编译」。所以源码里看到的 `zed::init(cx)`,指的是 `crate::zed` 模块里的函数,不是某个外部 crate。
- **命令行参数解析(clap)**:`clap` 是 Rust 最流行的命令行解析库。用 `#[derive(Parser)]` 标注的结构体 `Args`,调用 `Args::parse()` 就能把进程参数变成结构体字段。
- **闭包与回调**:Rust 的闭包(如 `move |cx| { ... }`)是可以「打包带走、以后再执行」的代码块。Zed 启动代码大量使用「注册回调,稍后触发」的写法。
- **应用上下文 `App` 与全局状态**:GPUI(第二单元详讲)用一个 `App` 类型的对象承载全局状态;`cx.set_global(x)` 把某个对象放进全局状态,之后任何地方都能 `X::global(cx)` 取出。本讲把它理解为「应用级的全局注册表」即可。
- **前台/后台任务**:`cx.spawn(...)` 的任务跑在 UI 主线程,`background_spawn` / `background_executor().spawn(...)` 跑在后台线程池。启动时一些耗时工作(如生成安装 ID)会提前丢到后台,启动后期再取回结果。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/zed/src/main.rs` | 二进制入口。`main()`、`Args` 定义、`app.run` 初始化闭包、工作区恢复逻辑都在这里,约 2000 行 |
| `crates/zed/src/zed.rs` | `main.rs` 的子模块,承载应用级逻辑:`zed::init`(注册全局 action)、`initialize_workspace`(工作区初始化)、设置/键位文件监听、主题预加载等 |
| `crates/zed/src/zed/open_listener.rs` | `OpenListener` 与「打开请求」(`RawOpenRequest`/`OpenRequest`)的定义,是 CLI 参数与已有实例之间传递「要打开什么」的信使 |
| `crates/gpui/src/app.rs` | GPUI 框架的 `Application` 类型定义(`with_platform`/`new_inaccessible`/`run`),本讲只看其中几行 |
| `crates/gpui_platform/src/gpui_platform.rs` | 平台检测入口 `current_platform`,按编译目标操作系统选择 Mac/Windows/Linux 平台实现 |
| `crates/release_channel/src/lib.rs` | 发布通道(dev/nightly/preview/stable)判定,影响单实例检查等行为 |

## 4. 核心概念与源码讲解

本讲的两个规定最小模块——「入口流程跟踪」与「初始化要点提炼」——分别由 4.1/4.2 和 4.3/4.4 四个小节承载。

### 4.1 入口流程跟踪:main() 的序曲

#### 4.1.1 概念说明

一个 GUI 程序的启动并不只是「打开窗口」这么简单。在窗口出现之前,Zed 要先回答几个问题:

- 我这次被调用,真的是要当编辑器吗?(也可能是 `--askpass`、`--crash-handler` 这类**旁路模式**,干完活就退出)
- 我要往哪些目录写数据?这些目录建得起来吗?(建不起来要给用户弹错误提示)
- 日志写到哪里? stdout 还是日志文件?
- 机器上是不是已经有一个 Zed 实例在跑?(单实例检查)

这一段代码全部在 `main()` 里、`app.run` 之前,是纯过程式的「顺序剧本」。读懂它,你就掌握了排查「Zed 打不开」问题的第一手地图。

#### 4.1.2 核心流程

`main()` 前半段的执行顺序(伪代码):

```text
main():
  记录启动时间 STARTUP_TIME
  sandbox 检查:若本进程是被重新执行为 Linux 沙箱助手,则运行该模式并直接返回
  (unix) 拒绝以 root 运行
  args = Args::parse()                    # clap 解析命令行
  if args.askpass:      运行 askpass 模式,返回   # 旁路模式
  if args.crash_handler: 运行崩溃处理服务,返回   # 旁路模式
  (windows) ETW 追踪等旁路模式...
  if args.printenv:     打印环境变量 JSON,返回   # 旁路模式
  if args.dump_all_actions: 导出全部 action,返回 # 旁路模式
  if args.user_data_dir: 设置自定义数据目录
  errors = init_paths()                    # 创建 config/extensions/logs 等目录
  if errors 非空: 弹窗报错并返回             # 目录都建不了,无法继续
  zlog::init()                             # 初始化日志
    stdout 是终端 → 日志走 stdout;否则走日志文件
  加载 AppVersion / AppCommitSha
  if args.system_specs: 打印系统规格,返回      # 又一个旁路模式
  构建 rayon 全局线程池(用于后台并行计算)
  log::info!("========== starting zed ...")  # 启动日志的「里程碑」行
  ... 进入 4.2 的 Application 构建
```

关键认知:**旁路模式都靠「提前 return」实现**。`--printenv`、`--system-specs` 这类调用根本不会创建窗口,甚至不会走到 `app.run`。

#### 4.1.3 源码精读

入口与前置检查——先记录启动时间、处理沙箱助手模式、拒绝 root、解析参数:

[crates/zed/src/main.rs:200-212](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L200-L212)

这段代码建立了「先特殊、后一般」的顺序:`sandbox::run_sandbox_launcher_if_invoked()` 必须放在参数解析**之前**,因为被包裹命令的参数会原样追加在后面,若先解析会被误认成 Zed 自己的参数(源码注释明确解释了这个原因)。

两个典型的旁路模式——`--askpass`(让 zed 充当 netcat 完成 SSH/Git 密码询问)与 `--crash-handler`(独立进程记录 minidump):

[crates/zed/src/main.rs:214-225](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L214-L225)

注意两者的写法:`askpass::main(socket); return;`——运行完旁路逻辑立刻返回,进程退出码由旁路逻辑决定。

目录初始化与日志决策——`init_paths()` 负责创建 8 个运行时目录,失败则收集错误;`zlog::init()` 之后按「stdout 是否为终端」决定日志去向:

[crates/zed/src/main.rs:287-304](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L287-L304)

`init_paths()` 的实现值得一看,它就是一张目录清单的折叠:

[crates/zed/src/main.rs:1656-1674](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L1656-L1674)

config、extensions、languages、debug_adapters、database、logs、temp、hang_traces——这 8 个目录就是 Zed 全部运行时数据的落点。如果创建失败(常见于权限问题),`files_not_created_on_launch` 会启动一个只含错误弹窗的最小窗口(见 [crates/zed/src/main.rs:95-150](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L95-L150)),而不是无声退出。

启动里程碑日志:

[crates/zed/src/main.rs:330-338](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L330-L338)

这一行 `========== starting zed version ..., sha ...` 是日志里区分「上次运行」与「本次运行」的锚点,也是你观察启动时机的第一站。

命令行参数结构体 `Args`(节选):

[crates/zed/src/main.rs:1686-1709](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L1686-L1709)

`paths_or_urls` 是不带 `--` 前缀的位置参数(`zed file.rs:10:5` 里的 `file.rs:10:5`);`--diff` 接受成对路径;`--user-data-dir` 允许把所有用户数据挪到自定义目录。结构体余下字段(`--system-specs`、`--askpass`、`--crash-handler`、Windows 专属的 `--wsl`/`--foreground` 等)见 [crates/zed/src/main.rs:1710-1793](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L1710-L1793)。

#### 4.1.4 代码实践

1. **实践目标**:用三个旁路模式命令,验证「不是每次运行 zed 都会出现窗口」。
2. **操作步骤**(在仓库根目录,沿用 u1-l2 的构建环境):
   1. `cargo run -p zed -- --printenv`,观察输出后进程退出。
   2. `cargo run -p zed -- --system-specs`,观察输出后进程退出。
   3. `cargo run -p zed -- --help`,阅读 clap 自动生成的帮助,对照 `Args` 结构体的 doc 注释。
3. **需要观察的现象**:`--printenv` 输出一段环境变量 JSON;`--system-specs` 输出 `Zed System Specs (from CLI):` 开头的系统规格;两者都不会弹出窗口,命令立即返回。
4. **预期结果**:三条命令都命中 4.1.2 流程图中的「旁路模式 → return」分支。`--help` 的每条参数说明都能在 `Args` 结构体([crates/zed/src/main.rs:1688-1793](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L1688-L1793))里找到对应的 doc 注释。
5. 若你的环境无法完成编译运行,此项「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `sandbox::run_sandbox_launcher_if_invoked()` 必须放在 `Args::parse()` 之前?

**答案**:因为当进程被重新执行为 Linux 沙箱助手时,被包裹命令的参数会原样追加在进程参数末尾;若先做参数解析,这些参数会被误认为是 Zed 自己的参数(可能触发 clap 报错或被当成要打开的文件)。源码注释([crates/zed/src/main.rs:203-206](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L203-L206))明确说明了这一点。

**练习 2**:`zed --printenv` 运行时,日志系统初始化了吗?窗口创建了吗?

**答案**:两种情况都不创建窗口,但日志状态不同:`--printenv` 的判断在 [crates/zed/src/main.rs:261-264](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L261-L264),位于 `zlog::init()`(293 行)**之前**,此时日志系统尚未初始化;`--system-specs` 的判断在 311-321 行,位于 `zlog::init()` 之后,日志系统已就位。两者最后都直接 `return`,不会走到 `app.run`。

**练习 3**:`init_paths()` 失败后 Zed 是直接退出吗?

**答案**:不是静默退出。错误被收集后交给 `files_not_created_on_launch`,它会构建一个只包含错误提示弹窗的最小窗口,把「哪个目录因何种错误创建失败」展示给用户;只有当连这个错误窗口都打不开时,才会走 `fail_to_open_window` 打印错误并退出([crates/zed/src/main.rs:95-197](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L95-L197))。

### 4.2 平台选择与 Application 构建:窗口世界的地基

#### 4.2.1 概念说明

Zed 是跨平台应用(macOS/Windows/Linux),但每个操作系统的窗口、事件循环、渲染后端完全不同。GPUI 的做法是定义一个 `Platform` **接口**(trait),再为每个系统各写一个实现。启动时要做的第一件「框架级」大事,就是**选出当前平台的实现**,用它构建 `Application` 对象——后者是 GPUI 世界的「应用壳」,持有平台句柄、事件循环与全局状态。

另外两个容易忽略的启动基石:

- **全局分配器**:`main.rs` 顶部用 `#[global_allocator]` 把 Rust 默认分配器换成 mimalloc(仅当启用 `mimalloc` feature),见 [crates/zed/src/main.rs:82-84](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L82-L84)。这是编译期全局生效的,属于「比 main 还早」的初始化。
- **单实例与崩溃处理**:正式版 Zed 通常只允许一个实例;崩溃时由独立进程记录 minidump。两者都在 `app.run` 之前就位。

#### 4.2.2 核心流程

```text
build_application():
  platform = gpui_platform::current_platform(false)
  if 环境变量 ZED_EXPERIMENTAL_A11Y == "1":
      Application::with_platform(platform)      # 保留系统辅助功能
  else:
      Application::new_inaccessible(platform)   # 默认关闭辅助功能(性能考虑)

app = build_application().with_assets(Assets)   # 挂载内嵌资源(图标/字体/默认配置)

# app.run 之前的三组准备:
  1. 数据库与 ID:app_db、system_id、installation_id、session 均在后台开始准备
  2. 单实例检查:
       正式版:Linux 尝试绑定 unix socket / Windows 命名管道 / macOS ensure_only_instance
       失败 → 打印 "zed is already running" 并退出
       dev 构建或 ZED_STATELESS:跳过检查
  3. 崩溃处理:按 release channel 决定安装 crash handler 或仅 force_backtrace

app.run(|cx| { ... }):                          # 事件循环启动后回调一次
  ← 本闭包即 4.3 的「初始化大本营」
```

`Application::run` 的语义很关键:它把闭包装箱后交给平台事件循环,**平台 `run` 会阻塞到应用生命周期结束**;闭包本身只在「应用完成启动」时被调用一次。

#### 4.2.3 源码精读

平台构建函数:

[crates/zed/src/main.rs:86-93](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L86-L93)

这段代码先向 `gpui_platform` 询问当前平台实现,再默认调用 `new_inaccessible`(关闭强制辅助功能)。只有设置环境变量 `ZED_EXPERIMENTAL_A11Y=1` 才走 `with_platform` 保留辅助功能。

平台检测的实现——按**编译目标操作系统**用 `cfg!` 选择,而不是运行时判断:

[crates/gpui_platform/src/gpui_platform.rs:57-75](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui_platform/src/gpui_platform.rs#L57-L75)

也就是说,macOS 构建里编译进去的是 `gpui_macos::MacPlatform`,Linux 构建里是 `gpui_linux` 的实现——跨平台差异在编译期就被裁剪掉了。

`Application::new_inaccessible` 与 `with_platform` 的关系:

[crates/gpui/src/app.rs:174-197](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L174-L197)

`new_inaccessible` 只是「构建 + 把辅助功能强制关闭标志置位」的组合快捷方式。

`Application::run` 的定义:

[crates/gpui/src/app.rs:224-236](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L224-L236)

读这段可以确认两件事:闭包参数类型是 `&mut App`(下讲反复出现的 `cx`);`platform.run(...)` 会阻塞整个应用生命周期,所以 `main()` 在 `app.run` 之后的代码要等应用退出才执行——事实上 main.rs 里 `app.run(...)` 就是 `main()` 的最后一句。

回到 main.rs:构建应用、挂载资源、提前在后台准备数据库与各类 ID:

[crates/zed/src/main.rs:343-354](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L343-L354)

注意 `system_id()`、`installation_id()`、`Session::new(...)` 都是 `background_executor().spawn` 出去的**后台任务**,它们的返回值(`Task`)会在 `app.run` 闭包里被 `block_on` 取回(见 4.3)。这是启动提速的典型手法:耗时 IO 与 UI 框架初始化并行。

单实例检查:

[crates/zed/src/main.rs:359-383](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L359-L383)

条件的第一支是 `*zed_env_vars::ZED_STATELESS || *release_channel::RELEASE_CHANNEL == ReleaseChannel::Dev`——**从源码构建的调试版属于 Dev 通道**(由 [crates/zed/RELEASE_CHANNEL](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/RELEASE_CHANNEL) 文件与 [crates/release_channel/src/lib.rs:13-19](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/release_channel/src/lib.rs#L13-L19) 决定),因此**你本地 `cargo run` 出来的 Zed 永远不做单实例检查**,每次运行都是新实例。Linux 正式版的机制是绑定一个 unix datagram socket(`zed-{channel}.sock`),见 [crates/zed/src/zed/open_listener.rs:409-432](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/zed/open_listener.rs#L409-L432):第二个实例绑定失败,打印 "zed is already running" 后退出;而 `zed file.rs` 命令行客户端则会把路径写进这个 socket,由第一实例代为打开。

崩溃处理的启动(节选):

[crates/zed/src/main.rs:385-420](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L385-L420)

是否安装 crash handler 由遥测配置按 release channel 决定;未安装时调用 `crashes::force_backtrace()` 至少保证崩溃时打印回溯。

#### 4.2.4 代码实践

1. **实践目标**:确认你机器上 Zed 使用的平台实现与 release channel。
2. **操作步骤**:
   1. 阅读上面的 `current_platform` 代码,写下你的操作系统对应的 `Platform` 实现类型名。
   2. 运行 `cat crates/zed/RELEASE_CHANNEL`,确认源码构建的通道名。
   3. (选做)用 `ZED_EXPERIMENTAL_A11Y=1 cargo run -p zed` 启动一次,与不带环境变量的启动对比;若你使用屏幕阅读器或系统辅助工具,可观察是否能读到 UI。
3. **需要观察的现象**:RELEASE_CHANNEL 文件内容为 `dev`;Linux 上 `current_platform` 走 `gpui_linux::current_platform(headless)` 分支。
4. **预期结果**:能回答「我的构建里,`Platform` trait 的实现来自哪个 crate」「为什么本地调试构建可以同时开多个 Zed 实例」两个问题。
5. 辅助功能的实际差异「待本地验证」(需要配套辅助技术观察)。

#### 4.2.5 小练习与答案

**练习 1**:`build_application()` 里为什么要区分 `with_platform` 和 `new_inaccessible`?

**答案**:`new_inaccessible` 会把 `accessibility_force_disabled` 置为 true,默认关闭辅助功能树以换取性能;`ZED_EXPERIMENTAL_A11Y=1` 时改走 `with_platform` 保留辅助功能,这是一个实验性开关([crates/gpui/src/app.rs:193-197](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L193-L197))。

**练习 2**:mimalloc 全局分配器是何时生效的?为什么它看起来「不在 main 里」?

**答案**:`#[global_allocator]` 是 Rust 语言级的属性,编译期把整个程序的堆分配器替换为 mimalloc,程序任何代码执行第一行之前它就已生效,所以不需要(也无法)在 `main()` 里调用。它只在启用 `mimalloc` feature 时编译进来([crates/zed/src/main.rs:82-84](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L82-L84))。

**练习 3**:`app.run(...)` 调用之后、`main()` 函数结束之前,还有别的语句吗?为什么?

**答案**:没有,`app.run(...)` 是 `main()` 的最后一句。因为 `Application::run` 内部的 `platform.run` 会阻塞到应用退出,写在它后面的语句要等应用完全退出才执行,对 GUI 程序没有意义。

### 4.3 初始化要点提炼:app.run 闭包里的注册大会

#### 4.3.1 概念说明

`app.run(move |cx| { ... })` 的闭包长达 500 多行(478-999 行),是整个 Zed 的「组装车间」。它的本质是一长串**注册**:

- 把准备好的对象放进全局状态(`cx.set_global`);
- 为每个功能模块调用 `xxx::init(cx)`,完成 action 注册、面板注册、事件监听等;
- 构造贯穿全局的 `AppState`(语言注册表、客户端、文件系统、会话等的大集合);
- 最后安排「打开什么」(第一个窗口从哪来)。

理解这段代码的钥匙是一个观察:**Zed 不用宏或自动发现来装配模块,而是手工按依赖顺序排列 init 调用**。顺序本身就是依赖关系的表达——设置必须先于一切读设置的模块,主题注册先于任何界面渲染,`workspace::init` 先于各种 UI 面板。这也呼应了 u1-l3 讲过的「DAG 依赖图」:闭包里的顺序就是这张图的一次拓扑排序。

#### 4.3.2 核心流程

把 500 行闭包按职责切成 6 段:

```text
app.run(|cx|):
  ① 全局基础:db 设为全局 → 受信 worktree → menu/zed_actions/release_channel/
     gpui_tokio → settings::init → watch_settings_files → handle_keymap_file_changes
  ② 网络与存储:http 客户端 → Fs 全局 → Git 托管注册表 → OpenListener 全局 →
     extension::init → Client::production → LanguageRegistry → node_runtime
  ③ 用户与语言:user_store/workspace_store → language_extension → zed::init →
     取回 system_id/installation_id/session(block_on)→ 遥测启动 →
     AppSession → 构造 AppState 并设为全局
  ④ 主题与扩展生态:auto_update → extension_host::init →
     theme_settings::init(注册内置主题)→ eager_load_active_theme_and_icon_theme →
     command_palette / copilot / language_models / agent_ui / repl ...
  ⑤ 编辑器与各 UI:load_embedded_fonts → editor::init → workspace::init →
     file_finder / project_panel / vim / terminal_view / git_ui / onboarding ... 数十个 init
     → 菜单(app_menus/set_menus)
  ⑥ 收尾:initialize_workspace → cx.activate(true) → authenticate 后台任务 →
     处理命令行传入的路径/URL → 安排第一个窗口(见 4.4)
```

其中与「主题、设置、扩展、会话」四个关键词直接对应的调用点,在下文源码精读中逐一给出。

#### 4.3.3 源码精读

闭包开头:数据库设为全局、菜单注册、设置初始化与设置/键位文件监听:

[crates/zed/src/main.rs:478-499](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L478-L499)

四个要点:`cx.set_global(app_db)` 把 `app.run` 之前创建的数据库接入全局;`settings::init(cx)`(496 行)是**设置系统就位的时刻**;`zed::watch_settings_files`(498 行)让 settings.json 的改动能热生效;`handle_keymap_file_changes`(499 行)同理负责 keymap.json。`watch_settings_files` 的实现在 zed.rs:

[crates/zed/src/zed.rs:2190-2209](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/zed.rs#L2190-L2209)

它把文件监听回调挂进 `SettingsStore`,文件每次变化都会通知设置错误、触发迁移事件。键位侧的 `handle_keymap_file_changes` 则会加载默认键位并在「基础键位/键盘布局/用户键位文件」任一变化时重建([crates/zed/src/zed.rs:2211-2270](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/zed.rs#L2211-L2270))。

网络、客户端与语言注册表(节选):

[crates/zed/src/main.rs:516-530](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L516-L530)

`<dyn Fs>::set_global` 之后全应用共用一个文件系统抽象;`Client::production(cx)` 创建到 zed 协作服务的客户端;`LanguageRegistry` 随后交由 `languages::init` 填充内置语言(561 行)。

取回后台准备的 ID 与会话、启动遥测:

[crates/zed/src/main.rs:595-605](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L595-L605)

这正是 4.2 中那三个后台任务的「收货点」:`block_on` 在这里等待结果(通常早已算完),随后 `telemetry.start(...)` 用这三个 ID 标记本次运行。

会话与 AppState 的构造——`AppState` 是后续所有工作区共享的「应用级物资箱」:

[crates/zed/src/main.rs:642-654](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L642-L654)

`AppSession::new` 包装了本次会话(含上次会话 ID,供「恢复上次会话」用);`AppState` 的七个字段(languages/client/user_store/fs/build_window_options/workspace_store/session)就是编辑器运转的最小集合。

主题与扩展生态的初始化:

[crates/zed/src/main.rs:660-675](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L660-L675)

`theme_settings::init(theme::LoadThemes::All(...))`(668 行)注册**内置主题**——这是「主题注册」的主调用;`eager_load_active_theme_and_icon_theme`(669 行)紧接着把用户设置里激活的主题(若来自扩展)提前同步加载,避免首帧闪默认主题,实现在 [crates/zed/src/zed.rs:2826-2860](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/zed.rs#L2826-L2860);668 行之后用户主题目录的加载与监听则在 [crates/zed/src/main.rs:844-846](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L844-L846)(`load_user_themes_in_background` + `watch_themes`)。

编辑器与 UI 功能的批量注册(节选):

[crates/zed/src/main.rs:727-787](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L727-L787)

从 `load_embedded_fonts`、`editor::init`、`workspace::init` 到 `vim::init`、`terminal_view::init`、`git_ui::init`、`onboarding::init`……这一段就是「crates/ 目录里那些 _ui、功能 crate 的入列仪式」。每个 init 内部通常注册 action、面板或设置监听,细节留待各单元。

`zed::init` 本身——注册全局 action(quit、打开设置文件、关于窗口等):

[crates/zed/src/zed.rs:194-203](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/zed.rs#L194-L203)

#### 4.3.4 代码实践

1. **实践目标**:量化「注册大会」的规模,并在真实运行中看到启动日志。
2. **操作步骤**:
   1. 统计 init 调用:`grep -c '::init(' crates/zed/src/main.rs`(只统计 main.rs;其中少数匹配来自 `mod` 之外的其他上下文,可再肉眼过滤)。
   2. 带日志启动:`RUST_LOG=info cargo run -p zed`,在终端输出里找到 `========== starting zed version ...` 行,以及其后的设置加载、语言注册等日志。
   3. 若从桌面启动(非终端),日志会写入文件;日志路径的确定逻辑见 [crates/zed/src/main.rs:295-303](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L295-L303)(`paths::log_file()`)。
3. **需要观察的现象**:终端能看到 `starting zed` 里程碑行及后续 INFO 日志;grep 统计出一个约几十次的 init 计数。
4. **预期结果**:对「一个功能 = 一个 crate + 一行 init」的装配风格建立直观量感;能区分「日志输出到 stdout」与「输出到文件」两种情形的触发条件(是否在终端运行)。
5. 具体日志条目与计数「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `settings::init(cx)` 必须排在绝大多数 `xxx::init` 之前?

**答案**:后面的模块在 init 时就要读取设置(如 532 行的 `cx.observe_global::<SettingsStore>` 回调里读 `ProjectSettings`,676 行读 copilot 配置)。设置系统没就位,任何读设置的初始化都会失败。闭包里的书写顺序 = 依赖顺序。

**练习 2**:用户把主题设为某个扩展提供的主题时,首帧前发生了什么?

**答案**:`theme_settings::init` 只注册内置主题;`eager_load_active_theme_and_icon_theme` 检查当前激活主题是否已在 `ThemeRegistry` 中,若没有则从扩展安装目录同步加载主题文件与图标主题文件([crates/zed/src/zed.rs:2826-2860](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/zed.rs#L2826-L2860)),避免窗口先用默认主题渲染再跳变。

**练习 3**:`AppState` 为什么用 `Arc` 包裹并设为全局?

**答案**:工作区、面板、Agent、终端等大量模块都需要访问同一组「语言注册表 + 客户端 + 文件系统 + 会话」。`Arc<AppState>` 允许跨线程、跨实体共享同一份不可变引用,`AppState::set_global` 后任何有 `cx` 的地方都能取到,避免层层传参。它的字段清单见 [crates/zed/src/main.rs:644-653](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L644-L653)。

### 4.4 打开第一个窗口:OpenRequest 与 restore_or_create_workspace

#### 4.4.1 概念说明

初始化完成后,「窗口从哪来」由一个三分支决策决定:

1. **命令行/其他实例带来了打开请求**(比如 `zed crates/rope`)→ 解析请求,打开对应路径;
2. **设置要求恢复上次会话/上次工作区** → 从本地数据库读出上次打开的工作区序列化快照,逐一恢复;
3. **首次启动**(数据库里没有 `FIRST_OPEN` 标记)→ 显示欢迎页(onboarding)。

支撑这套机制的两个角色:

- **`OpenListener`**:一个全局的「开门铃」。命令行参数、URL scheme(`zed://`)、第二实例转发,都会变成 `RawOpenRequest` 塞进它的 channel;主循环末尾有个循环不断消费这个 channel,处理启动之后陆续到来的打开请求。
- **`restore_or_create_workspace`**:三分支决策的函数体,也是「窗口出现」的最后一公里。

#### 4.4.2 核心流程

```text
闭包末段:
  initialize_workspace(app_state, cx)     # 工作区级钩子(4.3 的⑥)
  cx.activate(true)                       # 让应用来到前台
  spawn(authenticate(...))                # 后台静默登录
  args.paths_or_urls → parse_url_arg → open_listener.open(RawOpenRequest{urls,...})
                                          # 把本次命令行想打开的东西"按门铃"

  match open_rx.try_recv():               # 启动期间已经攒下的请求
    Some(且仅是聚焦请求) → restore_or_create_workspace
    Some(正常请求)      → handle_open_request(request)
    None                → restore_or_create_workspace

restore_or_create_workspace():
  if restorable_workspaces() 返回 Some:    # 设置 = 恢复上次工作区/会话
      对每个序列化的 MultiWorkspace:
        Local  → workspace::restore_multiworkspace(...)
        Remote → open_remote_project(...)
  else if 数据库无 FIRST_OPEN 键:          # 全新安装
      show_onboarding_view(...)            # 欢迎页窗口
  else:                                    # 既无可恢复也无首次标记
      workspace::open_new(...)             # 新建空工作区
        (restore_on_startup != Launchpad 时再新建一个空文件)

之后:后台循环消费 open_rx,处理运行期间到来的打开请求
```

#### 4.4.3 源码精读

「开门铃」的定义——`OpenListener` 就是一个多生产者 channel 的发送端,`RawOpenRequest` 描述「要打开什么」:

[crates/zed/src/zed/open_listener.rs:380-407](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/zed/open_listener.rs#L380-L407)

`impl Global for OpenListener {}` 一行让它可以挂到 GPUI 全局状态上(main.rs:521 的 `OpenListener::set_global`),于是任何模块(比如收到 `zed://` URL 的处理器)都能按响门铃。

命令行参数按下门铃——闭包末段把 `args.paths_or_urls` 转成 URL 并发送:

[crates/zed/src/main.rs:881-913](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L881-L913)

`parse_url_arg` 会把普通路径规范化为 `file://` URL,已是 `zed://`/`ssh://` 等 scheme 的保持原样([crates/zed/src/main.rs:1809-1825](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L1809-L1825))。注意时序:门铃按响在 904-913 行,**早于**下面 923 行的 `try_recv`——所以本次命令行想打开的东西会在启动时同步被取走处理,不会丢。

启动期的三分支调度:

[crates/zed/src/main.rs:923-948](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L923-L948)

三个分支分别对应「别的实例只想聚焦已有窗口」「带着真实打开请求」「什么都没带」。`handle_open_request` 是个大型 match,按请求类型(CLI 连接、聚焦、扩展安装、Agent 面板、git clone、普通路径、远程连接等)分派,主体在 [crates/zed/src/main.rs:1002-1257](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L1002-L1257)。

窗口出现的最后一公里——`restore_or_create_workspace` 的骨架:

[crates/zed/src/main.rs:1418-1431](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L1418-L1431)

从本地键值库读出可恢复的 `SerializedMultiWorkspace` 列表;本地工作区调 `workspace::restore_multiworkspace`,远程工作区走 `open_remote_project`(1432-1468 行)。恢复全部失败时会弹 Toast 或开兜底空窗口(1477-1551 行)。

首次启动与其余情况的兜底:

[crates/zed/src/main.rs:1552-1572](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L1552-L1572)

`FIRST_OPEN` 键不存在 → 欢迎页;否则新建空工作区,且仅当 `restore_on_startup` 设置不是 `Launchpad` 时顺手创建一个空文件(`Editor::new_file`)。可恢复位置如何选取(上次工作区 vs 整个会话)由 `restorable_workspace_locations` 决定:[crates/zed/src/main.rs:1585-1654](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L1585-L1654)。

首窗口的「到达信号」与后续请求循环:

[crates/zed/src/main.rs:950-957](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L950-L957)

用 `cx.observe_new::<MultiWorkspace>` 捕获第一个窗口实体创建的时机;[crates/zed/src/main.rs:980-998](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L980-L998) 则是常驻循环,处理启动之后源源不断的打开请求(macOS 冷启动时 `zed <path>` 会在恢复进行中到达,所以先等 `restore_finished` 或首窗口就位再匹配,源码注释引用了 issue #61346)。

`initialize_workspace` 做什么——为每个新建的 MultiWorkspace/Workspace 挂钩子(Sidebar、面板初始化、遥测定时器等):

[crates/zed/src/zed.rs:430-442](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/zed.rs#L430-L442)

注意它**不直接开窗口**,而是用 `cx.observe_new` 注册「将来任何 MultiWorkspace/Workspace 实体创建时」的回调([crates/zed/src/zed.rs:444-460](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/zed.rs#L444-L460))——真正的窗口创建发生在 4.4.2 流程图里的 restore/open_new 路径中。

#### 4.4.4 代码实践

1. **实践目标**:走通一条「命令行路径 → 窗口」的追踪链。
2. **操作步骤**:
   1. 阅读源码链:`main()` 的 `args.paths_or_urls`([crates/zed/src/main.rs:881-885](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L881-L885))→ `open_listener.open(RawOpenRequest{...})`(904-913 行)→ `open_rx.try_recv()`(923 行)→ `OpenRequest::parse` → `handle_open_request`(936-937 行)→ 普通 paths 分支的 `open_paths_with_positions`([crates/zed/src/zed/open_listener.rs:465-484](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/zed/open_listener.rs#L465-L484))。
   2. 实际运行一次:`cargo run -p zed -- crates/rope/src/rope.rs`,确认窗口打开并定位到该文件。
   3. 在设置里把 `restore_on_startup` 改为 `"none"`(用户 settings.json),再次无参数启动,对比窗口行为。
3. **需要观察的现象**:第 2 步窗口直接打开 rope.rs;第 3 步不再恢复上次工作区,而是走「新建空工作区(±空文件)」分支。
4. **预期结果**:你能不看讲义画出这条链的时序,并能解释为什么 `zed file.rs` 的请求不会丢(门铃在 `try_recv` 之前按响)。
5. `restore_on_startup` 的效果「待本地验证」(依赖你的用户数据目录状态)。

#### 4.4.5 小练习与答案

**练习 1**:为什么命令行打开请求要先经过 `OpenListener` channel,而不是在 `main()` 里直接打开?

**答案**:(a) 打开文件需要 `AppState` 等已初始化完毕的上下文,而解析参数发生在一切初始化之前;(b) 同一套「门铃」机制同时服务于启动后的第二实例转发、`zed://` URL、macOS 文件关联等场景(见 `app.on_open_urls`,[crates/zed/src/main.rs:455-464](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L455-L464));(c) 启动时用 `try_recv` 同步消费一次,再用常驻循环异步消费,两种时序统一处理。

**练习 2**:全新安装(无历史数据)第一次启动会看到什么?判断依据是哪一行?

**答案**:欢迎页(onboarding)。依据是 `restore_or_create_workspace` 中 `matches!(kvp.read_kvp(FIRST_OPEN), Ok(None))` 为真时调用 `show_onboarding_view`([crates/zed/src/main.rs:1552-1553](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L1552-L1553))——即数据库里尚无 `FIRST_OPEN` 键。

**练习 3**:`initialize_workspace` 与 `restore_or_create_workspace` 名字里都有 workspace,职责区别是什么?

**答案**:`initialize_workspace` 是**注册级**的——通过 `observe_new` 给将来所有工作区实体挂钩子(侧栏、面板、关闭确认等),不创建窗口;`restore_or_create_workspace` 是**实例级**的——决定此刻恢复/新建哪个工作区并真正创建窗口。前者在 main.rs:871 调用一次,后者是异步函数,在恢复路径中被 spawn 执行。

## 5. 综合实践

**任务**:在启动流程中插入三处 `eprintln!` 日志,验证你对「设置加载 → 主题注册 → 工作区创建」顺序的理解。

1. **实践目标**:亲手证明启动序列,而不是背结论;同时练习「改一行、编译、观察」的源码实验循环。

2. **操作步骤**:

   1. **插入点 A(设置加载)**:在 [crates/zed/src/main.rs:496](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L496) 的 `settings::init(cx);` 之后加一行:

      ```rust
      // 示例代码:启动顺序验证日志
      eprintln!("[startup-A] settings initialized");
      ```

   2. **插入点 B(主题注册)**:在 [crates/zed/src/main.rs:668-669](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L668-L669) 的 `theme_settings::init(...)` 与 `eager_load_active_theme_and_icon_theme(...)` 两行之后加:

      ```rust
      // 示例代码:启动顺序验证日志
      eprintln!("[startup-B] theme initialized");
      ```

   3. **插入点 C(工作区创建)**:在 [crates/zed/src/zed.rs:430](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/zed.rs#L430) `initialize_workspace` 函数体第一行加:

      ```rust
      // 示例代码:启动顺序验证日志
      eprintln!("[startup-C] initialize_workspace");
      ```

      再在 [crates/zed/src/main.rs:1418](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L1418) `restore_or_create_workspace` 函数体第一行加:

      ```rust
      // 示例代码:启动顺序验证日志
      eprintln!("[startup-D] restore_or_create_workspace");
      ```

   4. 编译并运行(在仓库根目录,沿用 u1-l2 的环境):`cargo run -p zed`。
   5. 观察终端输出顺序后,还原改动:`git checkout -- crates/zed/src/main.rs crates/zed/src/zed.rs`(本实验仅限本地,不要提交)。

3. **需要观察的现象**:终端应按顺序输出 `startup-A` → `startup-B` → `startup-C` → `startup-D`(A、B、C 在同一个闭包里顺序同步执行;D 所在函数由 spawn 的恢复任务稍后异步调用)。

4. **预期结果与解释**:
   - A(496 行)先于 B(668 行):设置系统是主题设置的读取前提。
   - B 先于 C(871 行调用 zed.rs:430):主题注册在 `initialize_workspace` 之前完成,首帧才能直接用正确主题渲染。
   - C 先于 D:`initialize_workspace` 只注册回调,真正开窗口的 `restore_or_create_workspace` 在其后的 spawn 任务里执行。
   - 补充细节:`eprintln!` 走 stderr,不经过 zlog,所以不会进日志文件,但一定出现在启动它的终端里;若你同时看到 `========== starting zed version ...`,那说明 INFO 日志也走了 stdout(终端启动情形)。

5. 各输出行的实际时序「待本地验证」——尤其当你的数据库里存在可恢复工作区时,D 之后还会出现恢复过程的其他输出。

## 6. 本讲小结

- `main()` 的前半段是顺序剧本:沙箱助手检查 → 参数解析 → 旁路模式提前返回 → 目录初始化 → 日志初始化 → 版本与线程池,任何一步失败都有明确的用户可见反馈。
- 窗口世界的地基由三件事奠定:`gpui_platform::current_platform` 按**编译目标**选择平台实现;`Application` 构建应用壳(`new_inaccessible` 默认关闭辅助功能);mimalloc 全局分配器在 `main` 之前就已生效。
- `app.run` 闭包是手工编排的「注册大会」:`cx.set_global` 放入全局对象、上百个 `xxx::init(cx)` 按依赖顺序注册各功能模块,顺序即依赖拓扑排序。
- 关键初始化锚点:设置 `settings::init`(main.rs:496)、主题 `theme_settings::init` + `eager_load_active_theme_and_icon_theme`(main.rs:668-669)、扩展宿主 `extension_host::init`(main.rs:660)、会话 `AppSession`(main.rs:642)、`AppState` 全局化(main.rs:654)。
- 第一个窗口来自三分支决策:启动期 `OpenListener` 门铃请求优先;否则按设置恢复上次工作区/会话;全新安装显示欢迎页;兜底新建空工作区。
- 你本地源码构建属 Dev 通道,单实例检查被跳过——调试时可同时开多个实例,这是实验时的重要前提。

## 7. 下一步学习建议

本讲的 `app.run` 闭包里反复出现 `App`、`cx.set_global`、`cx.observe_new`、`cx.spawn` 这些 GPUI 语法,我们只把它们当黑盒用了。下一讲 **u2-l1「GPUI 总览」** 将正式打开这个框架:模块划分、平台后端 crate、以及「UI 框架 + 并发运行时」的双重身份。建议在进入下一讲前,做两个热身阅读:

1. [crates/gpui/src/app.rs:174-236](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L174-L236) 中 `Application` 的其余 builder 方法(`with_assets`/`with_http_client` 等),感受「应用壳」还携带哪些能力;
2. 本讲 4.4 出现过的 `workspace::restore_multiworkspace` 与 `workspace::open_new` 位于 workspace crate——第四单元(u4-l3)会系统讲解,现在只需知道它们负责把序列化快照变回活的工作区窗口。
