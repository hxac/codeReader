# Deployer 与 DeploymentTask

## 1. 本讲目标

本讲是专家层「部署、Levers 与二次开发」的第一篇，回答一个贯穿前面八单元却一直被当作黑盒的问题：

> 词典的 `.bin`、方案的编译产物、用户词典的同步快照，到底是**谁、在什么时候、用什么线程**生成的？

学完本讲，你应该能够：

- 说清 `Deployer` 这个对象**持有哪些数据目录与分发信息**，以及它们各自承担什么职责。
- 区分 `RunTask`（同步执行）与 `ScheduleTask`（入队异步执行）两种调度方式的语义差异。
- 画出「一次 `start_maintenance` 如何在后台线程跑完整套部署任务」的完整时序，并解释维护模式期间为何前端拿不到会话。
- 理解 `std::async` + `std::future` + `std::mutex` 组成的极简线程模型，以及 `RIME_NO_THREADING` 单线程退化分支。

本讲只聚焦 `Deployer` 与 `DeploymentTask` 这两个**调度骨架**本身；具体有哪些部署任务（`workspace_update`、`schema_update`、`user_dict_sync` 等）由谁实现，留待下一篇 u9-l2 展开。

## 2. 前置知识

本讲假定你已经掌握以下概念（均为前几讲的结论，这里只做一句话复习）：

- **组件注册体系（u5-l1）**：`Class<T, Arg>::Require(name)` 按名查表取出工厂，再 `Create(arg)` 造出产品。`DeploymentTask` 也是这样注册和实例化的。
- **Messenger 信号（u2-l2 / u2-l4）**：`Deployer` 继承 `Messenger`，持有一条 `message_sink_` 信号；`Service` 在构造时把它接到 `Notify(0, ...)`，部署事件以 `session_id == 0` 表示「全局事件」推给前端。
- **智能指针别名（u4-l1）**：`the<T>` = `unique_ptr`，`an<T>`/`of<T>` = `shared_ptr`。本讲会解释为何 `RunTask` 用 `the` 而 `ScheduleTask` 用 `an`。
- **模块组（u5-l3）**：`kDeployerModules` = `core + dict + levers`，部署期加载的模块组（含 levers 里注册的所有 `DeploymentTask` 组件）。
- **C API 方法表（u1-l4）**：`RimeApi` 是一张函数指针表，`start_maintenance`、`join_maintenance_thread` 都是其中的槽位。

另外补充一点本讲会用到的 C++ 知识：

- **`std::any`**：C++17 的类型擦除容器，能装任意可拷贝类型的值，取出时用 `std::any_cast<T>` 还原类型。`TaskInitializer` 就是 `std::any` 的别名，用来给不同任务传不同形态的参数（一个路径、一组路径、一对字符串……）。
- **`std::async` / `std::future`**：`std::async(std::launch::async, lambda)` 在新线程里跑 lambda，返回的 `future` 可用来查询状态（`wait_for`）或阻塞等待（`get`）。本讲用它实现「后台跑任务」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/rime/deployer.h` | `DeploymentTask` 抽象基类与 `Deployer` 类的接口定义，本讲的主角。 |
| `src/rime/deployer.cc` | `Deployer` 的实现：任务队列、`Run()`、线程启动与回收。 |
| `src/rime/service.h` | `Service` 持有唯一的 `deployer_` 成员，并通过 `disabled()` 把维护模式与「能否创建会话」绑定。 |
| `src/rime/service.cc` | `Service` 构造时把 `deployer_.message_sink()` 接到全局 `Notify(0, ...)`。 |
| `src/rime/setup.cc` | `SetupDeployer()` 把 `RimeTraits` 里的目录与分发信息写入 `Deployer` 的字段（含回退默认值）。 |
| `src/rime_api_impl.h` | C API 的 `RimeStartMaintenance` / `RimeJoinMaintenanceThread` / `RimeSyncUserData` 等实现，是本讲代码实践的追踪入口。 |
| `src/rime_api.h` | `RimeApi` 方法表中 `start_maintenance` / `join_maintenance_thread` 等槽位的声明。 |

## 4. 核心概念与源码讲解

### 4.1 DeploymentTask：部署任务的抽象基类

#### 4.1.1 概念说明

在 librime 里，「部署」是一个泛指：编译方案、构建词典的 `.bin`、升级用户词典格式、清理日志、同步用户数据……这些活儿都被抽象成一个个**部署任务**。

`DeploymentTask` 是所有部署任务的统一基类。它本身不实现任何具体逻辑，只规定了一个契约：**给我一个 `Deployer*`，我返回一个 `bool` 表示成功与否**。这与引擎流水线四大组件（u5-l2）的设计哲学完全一致——基类定契约、子类填实现、用组件注册表按名装配。

它通过 `Class<DeploymentTask, TaskInitializer>` 继承了组件注册能力（u5-l1），因此每个具体任务（如 `workspace_update`）都是一个注册在 `Registry` 里的组件，靠 `DeploymentTask::Require(task_name)` 按名取出。

#### 4.1.2 核心流程

一个部署任务对象的生命周期只有两步：

1. **构造**：由工厂 `Create(arg)` 创建，`arg` 是 `TaskInitializer`（即 `std::any`），任务子类在构造函数里用 `std::any_cast<T>(arg)` 把参数还原成自己需要的类型。
2. **执行**：被调用 `Run(deployer)`，拿到 `Deployer*` 这个「执行环境」（里面有目录、分发信息、消息通道），完成具体工作并返回 `bool`。

伪代码：

```
任务对象 = DeploymentTask::Require("任务名") -> Create(arg)   // 造任务
是否成功 = 任务对象->Run(deployer)                              // 跑任务
```

#### 4.1.3 源码精读

`DeploymentTask` 的定义极其精炼，全部信息在 [deployer.h:24-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.h#L24-L30)：

```cpp
using TaskInitializer = std::any;

class DeploymentTask : public Class<DeploymentTask, TaskInitializer> {
 public:
  DeploymentTask() = default;
  virtual ~DeploymentTask() = default;
  virtual bool Run(Deployer* deployer) = 0;
};
```

要点逐行解读：

- `TaskInitializer = std::any`（[deployer.h:22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.h#L22)）：构造参数类型被擦除成 `std::any`，这样**同一个 `Class::Create(arg)` 接口**就能传递任意类型的参数。例如 `detect_modifications` 任务需要一组目录路径，它的 `arg` 是 `vector<path>`；而 `config_file_update` 任务需要文件名和版本键，`arg` 是 `pair<string,string>`（见 [rime_api_impl.h:73-78](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L73-L78) 与 [rime_api_impl.h:134](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L134)）。
- `Class<DeploymentTask, TaskInitializer>`：复用 u5-l1 的组件注册模板，`T=DeploymentTask`、`Arg=TaskInitializer`，于是每个任务子类只要 `new Component<MyTask>` 即可注册，并能被 `DeploymentTask::Require(name)` 找到。
- `virtual bool Run(Deployer* deployer) = 0`：唯一的核心纯虚函数。注意它**不返回结果数据**，只返回成功/失败，所有「环境」信息都通过 `Deployer*` 参数注入，避免任务各自维护全局状态。

#### 4.1.4 代码实践

**实践目标**：理解 `TaskInitializer` 如何让一个统一接口承载多种参数类型。

**操作步骤**：

1. 打开 [deployer.h:22-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.h#L22-L30)，确认 `TaskInitializer` 就是 `std::any`。
2. 打开 [rime_api_impl.h:65-89](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L65-L89)，观察 `RimeStartMaintenance` 里 `detect_modifications` 任务是如何用 `TaskInitializer args{vector<path>{...}};` 传参的。
3. 再看 [rime_api_impl.h:131-136](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L131-L136)，对比 `config_file_update` 用 `make_pair<string, string>` 传参。

**需要观察的现象**：同一个 `RunTask(name, arg)` 形参，实参类型却在不同调用点完全不同（`vector<path>` vs `pair<string,string>`），这正是 `std::any` 的作用。

**预期结果**：你会理解「为什么任务基类只接受一个 `std::any` 却能满足所有任务的参数需求」——参数的具体类型是任务子类与调用方之间的私有约定，基类不需要知道。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 `Run` 设计成 `Run()`（无参），而要传一个 `Deployer*`？

**参考答案**：因为任务需要访问执行环境——数据目录在哪里、分发信息是什么、完成后如何发消息通知前端。把这些集中放在 `Deployer` 里、以参数注入，比让每个任务自己去找全局单例更清晰，也便于测试时传入 mock 的 `Deployer`。

**练习 2**：`TaskInitializer` 为什么用 `std::any` 而不是模板或继承体系？

**参考答案**：组件注册表的 `Create(arg)` 是一个**非模板**的虚函数，签名必须固定。`std::any` 在保留固定签名的同时，把具体类型延迟到子类构造函数里用 `any_cast` 还原，是 C++ 里实现「固定接口 + 可变参数」的标准手法。

---

### 4.2 Deployer 的数据目录与分发信息

#### 4.2.1 概念说明

`Deployer` 首先是一个**配置容器**：它集中持有所有部署相关的外部环境信息。这些信息在库初始化（`SetupDeployer`）阶段被写入，之后在任务运行期间被当作「只读」访问（`deployer.h` 顶部注释明确写了 `read-only access after library initialization`）。

它持有的信息分三类：

1. **五个数据目录**：`shared_data_dir`、`user_data_dir`、`prebuilt_data_dir`、`staging_dir`、`sync_dir`——决定了「从哪里读源文件、往哪里写产物、备份/同步放哪里」。
2. **分发身份信息**：`distribution_name` / `distribution_code_name` / `distribution_version` / `app_name` / `user_id`——用于写 `__build_info`、判断安装是否更新、按用户隔离同步目录。
3. **行为开关**：`backup_config_files`——是否在部署时备份配置文件。

`Deployer` 还继承自 `Messenger`（u2-l4），从而持有一条 `message_sink_` 信号，作为部署任务向前端汇报进度的通道。

#### 4.2.2 核心流程

五个目录的职责与数据流向如下（箭头表示部署期写入方向）：

```
shared_data_dir   (系统/官方数据: *.yaml, *.bin, 预构建产物)
        │  读取源文件
        ▼
   [DictCompiler / 部署任务]
        │  写入产物
        ▼
staging_dir        (暂存: user_data_dir/build, 先写这里再验证)

prebuilt_data_dir  (预构建: shared_data_dir/build, 随发布包附带, 避免首次编译)

user_data_dir      (用户数据: 用户配置、用户词典)
        │
        ▼
sync_dir/user_id   (同步快照: user_data_sync_dir() = sync_dir / user_id)
```

目录的默认值与回退规则由构造函数和 `SetupDeployer` 共同决定：

- 构造时全部给「相对路径」兜底（`shared_data_dir="."`、`prebuilt_data_dir="build"` 等）。
- `SetupDeployer` 用前端传来的 `RimeTraits` 覆盖；若 `prebuilt_data_dir` 没提供，回退到 `shared_data_dir/"build"`；若 `staging_dir` 没提供，回退到 `user_data_dir/"build"`。

#### 4.2.3 源码精读

`Deployer` 的公有字段就是它的「全部家当」，见 [deployer.h:32-46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.h#L32-L46)：

```cpp
class Deployer : public Messenger {
 public:
  // read-only access after library initialization {
  path shared_data_dir;
  path user_data_dir;
  path prebuilt_data_dir;
  path staging_dir;
  path sync_dir;
  string user_id;
  string distribution_name;
  string distribution_code_name;
  string distribution_version;
  string app_name;
  bool backup_config_files;
  // }
```

注意几点：

- 五个 `path` 字段使用的是 `rime::path`（在 `common.h` 里定义为 `boost::filesystem::path` 或 `std::filesystem::path`，见 u1-l3），支持用 `/` 拼接子路径。
- 继承 `Messenger`（[deployer.h:32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.h#L32)）带来 `message_sink_`，供 `Run()` 发 `"deploy"/"start|success|failure"` 通知。

构造函数给出兜底默认值，见 [deployer.cc:15-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L15-L22)：

```cpp
Deployer::Deployer()
    : shared_data_dir("."),
      user_data_dir("."),
      prebuilt_data_dir("build"),
      staging_dir("build"),
      sync_dir("sync"),
      user_id("unknown"),
      backup_config_files(true) {}
```

真正的字段填充发生在 `SetupDeployer`，见 [setup.cc:57-81](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L57-L81)。其中目录回退逻辑尤其值得注意：

```cpp
if (RIME_PROVIDED(traits, prebuilt_data_dir))
  deployer.prebuilt_data_dir = path(traits->prebuilt_data_dir);
else
  deployer.prebuilt_data_dir = deployer.shared_data_dir / "build";
if (RIME_PROVIDED(traits, staging_dir))
  deployer.staging_dir = path(traits->staging_dir);
else
  deployer.staging_dir = deployer.user_data_dir / "build";
```

这说明：**预构建产物默认和官方数据放在一起**（`shared_data_dir/build`），可以随安装包分发；而**部署期产物默认落在用户目录下**（`user_data_dir/build`），不污染系统目录。这两条回退规则是理解「为什么首次部署后用户目录里会出现一个 `build` 文件夹」的关键。

按用户隔离的同步目录则是一个简单的方法，见 [deployer.cc:150-152](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L150-L152)：

```cpp
path Deployer::user_data_sync_dir() const {
  return sync_dir / user_id;
}
```

`Deployer` 是 `Service` 的成员（每个进程唯一一个），通过 `Service::deployer()` 访问，见 [service.h:84](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.h#L84) 与 [service.h:94](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.h#L94)：

```cpp
Deployer& deployer() { return deployer_; }
...
Deployer deployer_;
```

#### 4.2.4 代码实践

**实践目标**：把 `Deployer` 的五个目录字段与磁盘上的真实位置对应起来。

**操作步骤**：

1. 用 `grep` 在 `rime_api_console.cc` 或任意前端代码里搜索 `shared_data_dir` / `user_data_dir`，找到它们通常被设成什么值（macOS 上 Squirrel 常用 `~/Library/Rime`，Linux 上常见 `~/.config/ibus/rime`）。
2. 在 [setup.cc:57-81](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L57-L81) 里确认：如果前端**没有**显式提供 `staging_dir`，它会回退到哪个路径？
3. 找到你本机的 Rime 用户数据目录，看里面是否有一个 `build` 子目录（这就是 `staging_dir` 的产物）。

**需要观察的现象**：用户目录下的 `build` 目录里通常有 `*.table.bin`、`*.prism.bin`、`*.schema.yaml` 等，它们都是部署任务写到 `staging_dir` 的结果。

**预期结果**：你能画一张表，把 `shared/user/prebuilt/staging/sync` 五个字段映射到本机的真实绝对路径。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `prebuilt_data_dir` 默认指向 `shared_data_dir/build`，而 `staging_dir` 默认指向 `user_data_dir/build`？

**参考答案**：预构建产物（prebuilt）是随安装包分发的、所有用户共享的只读 `.bin`，自然放在系统级的 `shared_data_dir` 下；而 staging 是本机部署期新产生的、可能因用户定制而不同的产物，必须放在可写的 `user_data_dir` 下，二者物理隔离避免了权限和污染问题。

**练习 2**：`Deployer` 继承 `Messenger` 的意义是什么？

**参考答案**：让 `Deployer` 拥有一条 `message_sink_` 信号通道，部署任务在开始/完成时可以通过 `deployer->message_sink_(type, value)` 通知前端；`Service` 构造时把这条信号接到全局 `Notify(0, ...)`（见 [service.cc:67-70](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L67-L70)），前端就能收到 `session_id=0` 的 `"deploy"/"start|success|failure"` 事件。

---

### 4.3 任务队列：RunTask / ScheduleTask / NextTask

#### 4.3.1 概念说明

有了「任务」和「环境」，还需要一个**调度器**把它们串起来。`Deployer` 内部维护一个 `std::queue<of<DeploymentTask>>`（FIFO 任务队列），并提供两种截然不同的调度方式：

- **`RunTask(name, arg)`**：**同步**执行——当场造任务、当场 `Run`、当场返回结果。任务对象用 `unique_ptr`（`the<>`）持有，跑完即销毁。用于「必须立刻完成、后续步骤依赖它」的前置任务（如 `installation_update` 失败就直接返回）。
- **`ScheduleTask(...)`**：**入队**而非执行——把任务对象塞进 `pending_tasks_` 队列，等待后台线程统一跑。任务对象用 `shared_ptr`（`an<>`/`of<>`）持有，因为它的生命周期要跨越「入队」与「在另一线程被取出执行」两个阶段。用于可批量异步处理的任务。

这二者的区分是本模块的核心：`the` vs `an` 的选择不是风格问题，而是由「谁来管理任务对象的生命周期」决定的。

#### 4.3.2 核心流程

同步路径（`RunTask`）：

```
RunTask("xxx", arg)
  -> DeploymentTask::Require("xxx")  查注册表拿工厂 c
  -> c->Create(arg)                  造出任务 t（unique_ptr，本函数独占）
  -> t->Run(this)                    同步执行
  -> 返回 bool，函数结束 t 自动析构
```

异步路径（`ScheduleTask`）：

```
ScheduleTask("yyy", arg)
  -> Require("yyy") -> Create(arg)   造出任务 t（shared_ptr）
  -> ScheduleTask(t)                 加锁，t 入队 pending_tasks_
  -> 返回 true（注意：不返回任务的执行结果！）

   ……稍后，在 Run() 的循环里……
NextTask()
  -> 加锁，取队首、pop，返回 shared_ptr（可能为空）
  -> t->Run(this)
```

关键细节：`ScheduleTask` **不知道任务会不会成功**，它只负责「排队」。真正的执行和结果统计发生在 `Run()`（见 4.4）。

#### 4.3.3 源码精读

先看同步执行的 `RunTask`，[deployer.cc:28-40](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L28-L40)：

```cpp
bool Deployer::RunTask(const string& task_name, TaskInitializer arg) {
  auto c = DeploymentTask::Require(task_name);
  if (!c) {
    LOG(ERROR) << "unknown deployment task: " << task_name;
    return false;
  }
  the<DeploymentTask> t(c->Create(arg));   // unique_ptr：本函数独占
  if (!t) {
    LOG(ERROR) << "error creating deployment task: " << task_name;
    return false;
  }
  return t->Run(this);                      // 同步跑，直接返回结果
}
```

注意三处容错：找不到任务名（`!c`）、造任务失败（`!t`）、任务本身返回 `false`，都会让 `RunTask` 返回 `false`，调用方据此决定是否中止后续步骤（如 [rime_api_impl.h:69-71](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L69-L71) 里 `installation_update` 失败就立即 `return False`）。

再看异步的 `ScheduleTask`，它有两个重载。第一个「按名入队」封装了「造任务」步骤，[deployer.cc:42-55](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L42-L55)：

```cpp
bool Deployer::ScheduleTask(const string& task_name, TaskInitializer arg) {
  auto c = DeploymentTask::Require(task_name);
  ...
  an<DeploymentTask> t(c->Create(arg));    // shared_ptr：要跨线程
  ...
  ScheduleTask(t);
  return true;                              // 只保证入队成功，不保证执行结果
}
```

第二个「直接入队已造好的任务」是底层操作，[deployer.cc:57-60](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L57-L60)：

```cpp
void Deployer::ScheduleTask(an<DeploymentTask> task) {
  std::lock_guard<std::mutex> lock(mutex_);
  pending_tasks_.push(task);
}
```

这里**加锁**是因为 `ScheduleTask` 可能在主线程被调用，而 `NextTask` 可能在工作线程消费队列。锁保护的是共享的 `pending_tasks_`。

出队操作 `NextTask` 同样加锁，[deployer.cc:62-72](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L62-L72)：

```cpp
an<DeploymentTask> Deployer::NextTask() {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!pending_tasks_.empty()) {
    auto result = pending_tasks_.front();
    pending_tasks_.pop();
    return result;
  }
  // there is still chance that a task is added by another thread
  // right after this call... careful.
  return nullptr;
}
```

注意那句注释：`NextTask` 返回 `nullptr` **不代表队列永远不会再有任务**——另一线程可能在此刻刚塞进来一个。这正是 `Run()` 要用 `do-while` + `HasPendingTasks()` 双重检查的原因（见 4.4.3）。

队列本身与锁的定义在 [deployer.h:71-72](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.h#L71-L72)：

```cpp
std::queue<of<DeploymentTask>> pending_tasks_;
std::mutex mutex_;
```

#### 4.3.4 代码实践

**实践目标**：在真实调用点辨认「同步 vs 异步」两种调度，并理解它们的组合方式。

**操作步骤**：

1. 打开 [rime_api_impl.h:65-89](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L65-L89)（`RimeStartMaintenance`）。
2. 找出其中调用的 `RunTask`（同步）有哪些：`clean_old_log_files`、`installation_update`、`detect_modifications`。
3. 找出其中调用的 `ScheduleTask`（异步入队）有哪些：`workspace_update`、`user_dict_upgrade`、`cleanup_trash`。
4. 思考：为什么前三步用 `RunTask`、后三步用 `ScheduleTask`？

**需要观察的现象**：前三步是「前置检查与准备」——它们的成败决定是否继续；后三步是「实际部署工作」——可以打包丢给后台线程慢慢跑。

**预期结果**：你会总结出一条经验：**有依赖、需判成败的前置任务用 `RunTask` 同步串起来；无强依赖、耗时的主体任务用 `ScheduleTask` 异步入队**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `RunTask` 用 `the<DeploymentTask>`（unique_ptr）而 `ScheduleTask` 用 `an<DeploymentTask>`（shared_ptr）？

**参考答案**：`RunTask` 造出的任务在当前函数内同步 `Run` 完就销毁，独占所有权，`unique_ptr` 足矣且开销最小；`ScheduleTask` 的任务对象要先入队、再由另一个线程取出执行，跨越函数返回和线程边界，需要 `shared_ptr` 的共享所有权来延长生命周期到真正执行的那一刻。

**练习 2**：如果调用 `ScheduleTask("typo_name")` 传了一个拼错的任务名，会发生什么？

**参考答案**：`Require` 找不到，记一条 `ERROR` 日志 `unknown deployment task` 并返回 `false`，不会有任务入队，也不会崩溃。这与引擎装配组件「缺件仅记 ERROR 跳过」的容错策略一致（u6-l1）。

---

### 4.4 线程模型与维护模式：StartWork / StartMaintenance / Join

#### 4.4.1 概念说明

任务入队之后，谁来执行它们？`Deployer` 用一个极简的线程模型：**一条工作线程 + 一个 `std::future` 句柄**。核心方法有三组：

- **`Run()`**：消费循环——反复 `NextTask` 取出任务执行、统计成功失败、发消息通知，直到队列空。这是工作线程的入口。
- **`StartWork(maintenance_mode)` / `StartMaintenance()`**：启动——用 `std::async` 在新线程里跑 `Run()`，把句柄存进 `work_`（`std::future`）。`StartMaintenance()` 只是 `StartWork(true)` 的别名。
- **`IsWorking()` / `IsMaintenanceMode()`**：探测——前者查 `work_` 是否还在跑，后者额外要求 `maintenance_mode_` 为真。
- **`JoinWorkThread()` / `JoinMaintenanceThread()`**：阻塞等待工作线程结束（二者等价，都调用 `work_.get()`）。

**维护模式（maintenance mode）** 是本模块最重要的概念。当 `StartMaintenance()` 被调用，`maintenance_mode_` 置真，于是 `Service::disabled()` 返回真（[service.h:85](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.h#L85)），这期间**前端无法创建新会话、已有会话也拒绝工作**。设计意图是：部署期正在改写 `.bin` 和配置，此时让用户输入会读到半成品，干脆「停服」直到部署完成。维护模式结束后，`disabled()` 恢复 false，前端又能正常使用。

#### 4.4.2 核心流程

一次完整维护的时序（对应代码实践要追踪的 `start_maintenance`）：

```
前端: api->start_maintenance(full_check)
       │
       ▼  (rime_api_impl.h: RimeStartMaintenance)
  LoadModules(kDeployerModules)                 加载部署模块组(注册任务组件)
  RunTask("clean_old_log_files")                ─┐ 同步前置
  RunTask("installation_update")                 │
  if (!full_check) RunTask("detect_modifications")─┘ 失败即返回
  ScheduleTask("workspace_update")              ─┐ 异步入队
  ScheduleTask("user_dict_upgrade")              │
  ScheduleTask("cleanup_trash")                 ─┘
  StartMaintenance() == StartWork(true)
       │
       ▼  (deployer.cc: StartWork)
  maintenance_mode_ = true
  work_ = std::async(std::launch::async, []{ Run(); })   开后台线程
       │
       ▼  (工作线程: Run)
  message_sink_("deploy","start")               通知前端「开始」
  循环: NextTask() -> task->Run(this) -> 统计 success/failure
  message_sink_("deploy","success"|"failure")   通知前端「结束」
  do-while(HasPendingTasks())                   防止漏掉执行中新入队任务
       │
       ▼  (前端, 在 IsWorking() 变 false 后)
  disabled() 恢复 false, 可重新创建会话
```

线程安全要点：`Run()` 跑在工作线程，而 `ScheduleTask` 可能从主线程随时入队，二者通过 `mutex_` 协调对 `pending_tasks_` 的访问；`message_sink_` 的通知最终经 `Service::Notify`（自身带锁）传给前端。

#### 4.4.3 源码精读

先看 `StartWork`，[deployer.cc:106-124](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L106-L124)：

```cpp
bool Deployer::StartWork(bool maintenance_mode) {
  if (IsWorking()) {
    LOG(WARNING) << "a work thread is already running.";
    return false;                              // 已有线程在跑,拒绝重入
  }
  maintenance_mode_ = maintenance_mode;
  if (pending_tasks_.empty()) {
    return false;                              // 没任务可跑
  }
#ifdef RIME_NO_THREADING
  LOG(INFO) << "running " << pending_tasks_.size() << " tasks in main thread.";
  return Run();                                // 无线程支持,主线程同步跑
#else
  LOG(INFO) << "starting work thread for " << pending_tasks_.size() << " tasks.";
  work_ = std::async(std::launch::async, [this] { Run(); });
  return work_.valid();
#endif
}
```

关键点：

- **防重入**：已有工作线程在跑就直接返回 false，避免多个线程同时操作 `pending_tasks_` 的消费者端。
- **`RIME_NO_THREADING` 退化分支**：在禁用线程的构建配置下（如某些受限平台），退化为在**主线程同步**跑 `Run()`，语义不变但会阻塞调用方。注意此分支读 `pending_tasks_.size()` 没加锁，因为此时不存在并发。
- **`std::launch::async`** 强制新线程立即启动（而非可能的延迟到 `get()` 才跑），`work_.valid()` 返回 true 表示成功派发。

`StartMaintenance` 只是套了一层，[deployer.cc:126-128](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L126-L128)：

```cpp
bool Deployer::StartMaintenance() {
  return StartWork(true);
}
```

状态探测的两个函数体现了 `future` 的用法，[deployer.cc:130-139](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L130-L139)：

```cpp
bool Deployer::IsWorking() {
  if (!work_.valid())
    return false;
  auto status = work_.wait_for(std::chrono::milliseconds(0));  // 不阻塞
  return status != std::future_status::ready;
}

bool Deployer::IsMaintenanceMode() {
  return maintenance_mode_ && IsWorking();
}
```

`wait_for(0)` 是「轮询而不阻塞」的标准写法：超时 0 毫秒立即返回当前状态，若尚未 `ready` 说明工作线程还在跑。`IsMaintenanceMode` 要求**既是维护模式、又确实在跑**——这保证维护一结束（线程退出），`disabled()` 立刻恢复 false。

回收线程的两个 Join 实际等价，[deployer.cc:141-148](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L141-L148)：

```cpp
void Deployer::JoinWorkThread() {
  if (work_.valid())
    work_.get();                               // 阻塞至工作线程结束
}

void Deployer::JoinMaintenanceThread() {
  JoinWorkThread();
}
```

`work_.get()` 会阻塞直到异步任务完成（同时会抛出 lambda 里未捕获的异常，但 `Run()` 内部已用 try/catch 兜住，见下）。注意析构函数也会调 `JoinWorkThread()`（[deployer.cc:24-26](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L24-L26)），保证 `Deployer` 销毁前工作线程一定已结束。

最核心的是消费循环 `Run()`，[deployer.cc:79-104](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L79-L104)：

```cpp
bool Deployer::Run() {
  LOG(INFO) << "running deployment tasks:";
  message_sink_("deploy", "start");
  int success = 0;
  int failure = 0;
  do {
    while (auto task = NextTask()) {
      try {
        if (task->Run(this))
          ++success;
        else
          ++failure;
      } catch (const std::exception& ex) {
        ++failure;
        LOG(ERROR) << "Error deploying: " << ex.what();
      }
      // boost::this_thread::interruption_point();
    }
    LOG(INFO) << success + failure << " tasks ran: " << success << " success, "
              << failure << " failure.";
    message_sink_("deploy", !failure ? "success" : "failure");
    // new tasks could have been enqueued while we were sending the message.
  } while (HasPendingTasks());
  return !failure;
}
```

逐层拆解：

1. **开始通知**：`message_sink_("deploy", "start")` 通知前端部署开始（前端据此可显示「正在部署…」）。
2. **内层 while**：反复取任务执行。每个任务用 try/catch 兜住异常，**单个任务抛异常只计 failure、不会让整个部署崩溃**——这是健壮性设计。
3. **结束通知**：队列空后，根据是否有 failure 发 `"success"` 或 `"failure"`。
4. **外层 do-while**：`HasPendingTasks()` 的二次检查。为什么需要它？因为「发消息、记日志」的瞬间，另一线程可能又 `ScheduleTask` 进来新任务；直接 return 会漏掉它们，所以再循环一次。这与 `NextTask` 的注释（「另一线程随时可能入队」）呼应。

`Service::disabled()` 把维护模式与会话可用性绑死，[service.h:85](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.h#L85)：

```cpp
bool disabled() { return !started_ || deployer_.IsMaintenanceMode(); }
```

`CreateSession` 一开头就检查 `disabled()`（见 u2-l2），故维护期间会话创建被拒。

#### 4.4.4 代码实践

**实践目标**：端到端追踪「`start_maintenance` 如何触发 `Deployer` 在后台线程跑部署任务」——这是本讲规格指定的核心实践。

**操作步骤**：

1. 从 C API 表项出发：[rime_api.h:291-293](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L291-L293) 是 `start_maintenance` / `is_maintenance_mode` / `join_maintenance_thread` 三个槽位的声明。
2. 找到它的实现绑定：[rime_api_impl.h:1140](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L1140) 处 `s_api.start_maintenance = &RimeStartMaintenance`（[rime_api_impl.h:1142](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L1142) 绑定 `join_maintenance_thread`）。
3. 读 `RimeStartMaintenance` 主体：[rime_api_impl.h:65-89](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L65-L89)。按顺序标注它做了什么：
   - `LoadModules(kDeployerModules)`：确保 levers 模块组已加载，所有部署任务组件已注册。
   - 两个 `RunTask`（同步）：清旧日志、安装更新（失败即返回）。
   - `full_check == false` 时再 `RunTask("detect_modifications", ...)` 检测改动。
   - 三个 `ScheduleTask`（入队）：工作区更新、用户词典升级、清垃圾。
   - `deployer.StartMaintenance()`：启动后台线程。
4. 跟进 `StartMaintenance` → `StartWork(true)` → `std::async(... Run() ...)`，再到 `Run()` 的消费循环，最后到 `message_sink_("deploy", ...)` 经 `Service::Notify(0,...)` 推给前端。
5. （可选）如果你已按 u1-l5 编译了 `rime_api_console`，可在控制台运行它，首次启动时会观察到部署日志与 `"deploy"/"start"` → `"deploy"/"success"` 的通知；若无法本地运行，本步标注「待本地验证」。

**需要观察的现象**：部署期间若试图 `create_session`，会因 `disabled()` 返回真而失败；部署完成后恢复正常。

**预期结果**：你能画出一条从 `api->start_maintenance(False)` 到后台线程 `Run()` 再到前端 `on_message(session_id=0, "deploy", "success")` 的完整调用链。

#### 4.4.5 小练习与答案

**练习 1**：`Run()` 为什么用 `do { while(...) ... } while (HasPendingTasks())` 而不是单个 `while(NextTask())`？

**参考答案**：因为单个 `while` 在队列空时就会退出并返回，但此时另一线程可能恰好刚 `ScheduleTask` 入队了新任务（如某任务执行中又触发了新的部署任务）。外层 `do-while(HasPendingTasks())` 在内层循环退出、发完通知后再次检查队列，确保「执行中新入队」的任务不会被遗漏。

**练习 2**：维护模式期间，前端调用 `process_key` 会怎样？为什么这样设计？

**参考答案**：维护期间 `disabled()` 为真，`create_session` 会被拒，已无有效会话，`process_key` 自然拿不到 session 而失败。即便有残留会话，部署期正在重写 `.bin` 与配置，此时让用户输入可能读到不一致的半成品，故设计成「停服」直到部署完成，以一致性优先。

**练习 3**：`StartWork` 里有 `#ifdef RIME_NO_THREADING` 分支，它的意义是什么？

**参考答案**：为不支持多线程（或刻意禁用线程）的构建目标提供退路——此时不派发后台线程，而在调用方线程同步执行 `Run()`。这保证 librime 在受限平台上仍可完成部署，代价是部署期间阻塞主线程。

## 5. 综合实践

**任务**：画出 `RimeSyncUserData` 的完整执行链，并与 `RimeStartMaintenance` 对比。

`RimeSyncUserData`（[rime_api_impl.h:138-145](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L138-L145)）是另一个会触发维护的 C API。请完成：

1. 读这段代码，列出它 `ScheduleTask` 了哪三个任务（`installation_update`、`backup_config_files`、`user_dict_sync`），并指出它在调度前额外调了哪个 `Service` 方法（`CleanupAllSessions`）。
2. 它同样调用 `StartMaintenance()`，因此也会进入维护模式、`disabled()` 为真——解释为什么同步用户数据前要先 `CleanupAllSessions`（提示：用户词典正在被读写/导出，必须先释放所有会话对它的占用）。
3. 用一张表对比 `RimeStartMaintenance` 与 `RimeSyncUserData`：它们的同步前置步骤、入队任务、是否进入维护模式各有何异同。
4. （进阶）结合 4.4.3 的 `Run()` 消费循环，说明这两个 API 入队的任务最终都会被**同一段** `while(NextTask())` 代码消费——这正是「统一调度器」的威力：不同的入口（维护 / 同步）只是往同一个队列塞不同的任务，执行机制完全复用。

**预期产出**：一张调用链图 + 一张对比表 + 一段对「统一调度器」设计优势的说明。

## 6. 本讲小结

- `DeploymentTask` 是所有部署任务的抽象基类，唯一契约是 `Run(Deployer*) -> bool`，通过 `Class<DeploymentTask, std::any>` 复用组件注册，靠 `DeploymentTask::Require(name)` 按名实例化，用 `std::any` 擦除参数类型以支持千差万别的任务入参。
- `Deployer` 首先是配置容器：持有 `shared/user/prebuilt/staging/sync` 五个数据目录、分发身份信息与 `backup_config_files` 开关；`prebuilt_data_dir` 默认回退到 `shared_data_dir/build`，`staging_dir` 默认回退到 `user_data_dir/build`，由 `SetupDeployer` 在初始化期一次性写入。
- 调度分两路：`RunTask` 同步执行（任务用 `unique_ptr` 独占，跑完即销毁，适合需判成败的前置步骤）；`ScheduleTask` 入队异步执行（任务用 `shared_ptr`，生命周期跨线程，适合批量主体工作）。
- 线程模型极简：一条工作线程，由 `std::async` 启动、`std::future`（`work_`）持有；`Run()` 是消费循环，用 try/catch 保证单任务异常不拖垮整体，用 `do-while(HasPendingTasks())` 兜住执行中新入队的任务。
- **维护模式**由 `StartMaintenance() == StartWork(true)` 进入，期间 `maintenance_mode_ && IsWorking()` 为真，`Service::disabled()` 返回真，前端无法创建会话——以「停服」换取部署期数据一致性。
- 通知通道复用 `Messenger`：`Run()` 通过 `message_sink_("deploy","start|success|failure")` 汇报进度，经 `Service::Notify(0,...)` 以 `session_id=0` 全局事件推给前端。

## 7. 下一步学习建议

本讲只搭起了部署的**调度骨架**——`Deployer` 怎么排队、怎么开线程、怎么进维护模式。但队列里那些任务名（`workspace_update`、`schema_update`、`user_dict_sync`、`detect_modifications`……）到底各自做什么，我们一直当作黑盒。下一篇 **u9-l2「部署任务族」** 会逐个拆开 levers 模块注册的 `DeploymentTask` 子类：

- `DetectModifications` 如何靠校验和判断要不要重建。
- `WorkspaceUpdate` / `SchemaUpdate` 如何驱动 `DictCompiler`（承接 u8-l1~u8-l4 的词典构建）。
- `UserDictUpgrade` / 用户词典同步如何与 u8-l6 的 `user_db` 联动。

建议阅读顺序：先回顾 u8-l1 的 `DictCompiler::Compile` 四步产物，再读本讲，最后进 u9-l2，就能把「词典编译 → 任务封装 → 调度执行」三层彻底打通。后续 u9-l3 的 `Customizer`、u9-l4 的 `Switcher` 也都建立在 `Deployer` 提供的目录与调度能力之上。
