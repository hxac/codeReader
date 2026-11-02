# 做种与退出：种子条件判定

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 aria2 在 BT 下载完成后**为什么不立即退出**，以及由哪些对象决定「做种何时结束」。
2. 解释 `ShareRatioSeedCriteria` 的分享率计算公式，理解分子为什么是「历史上传量 + 本次会话上传量」。
3. 理解 `UnionSeedCriteria` 如何用「OR 语义 + 短路求值」组合多个退出条件，以及 `BtSetup` 如何依据 `--seed-time` / `--seed-ratio` 两个选项决定装配出哪些条件。
4. 跟踪 `SeedCheckCommand` 这个常驻命令的裁定循环，以及 `BtRuntime::setHalt(true)` 如何像广播一样让整套 BT 命令优雅退场。
5. 区分两种容易混淆的「完成」：`.torrent` 文件任务完成后由 `BtPostDownloadHandler` 派生出真正的数据任务；数据任务完成后进入做种态，由 `enableSeedOnly` 等机制完成状态切换。

## 2. 前置知识

**做种（seeding）**：BT 网络的共识是「下载完不等于结束」。你下载完一个种子后，aria2 会继续把数据上传给其他还在下载的 peer，这个过程叫做种。做种是 BT 生态能运转的根基——如果人人下完就走，后来者将无数据可下。

**分享率（share ratio）**：衡量「贡献了多少」的指标，定义为累计上传字节数除以该种子总字节数。分享率达到 1.0 意味着你至少向外送出了与文件等量的数据。

**为什么需要一个「判定框架」**：做种要持续多久，用户可能有不同诉求——「传够 2.0 倍就走」（按比例）、「传 30 分钟就走」（按时长）、或者「两个条件满足其一就走」（组合）。aria2 把这些诉求抽象成一组可组合的小对象，本讲的 `SeedCriteria` 家族就是这套抽象。

**你需要先具备的认知**（来自前置讲义）：

- u2-l1：`DownloadEngine` 主循环中，命令 `execute()` 返回 `false` 必须自我重排，返回 `true` 即销毁；`STATUS_REALTIME` 状态的命令每轮循环都会被执行。
- u3-l1：`RequestGroup` 是一次下载任务的容器，`numCommand` 归零是任务结束的判据；任务完成后可经 `PostDownloadHandler` 派生新任务，形成 `followedBy`/`following` 链。
- u6-l2：每个种子在 `BtRegistry` 中注册了「六件套」，其中 `BtRuntime` 是种子级运行时状态（连接数、停机标志等）的持有者，`BtSetup::setup` 负责装配种子级周期命令。
- u6-l4：`PeerInteractionCommand` 是每个 peer 会话的常驻宿主命令。

**组合模式（Composite pattern）**：一种设计模式——让「单个条件」和「条件的组合」实现同一个接口，调用方无需区分自己拿到的是叶子还是容器。本讲的 `UnionSeedCriteria` 就是组合节点，`TimeSeedCriteria`/`ShareRatioSeedCriteria` 是叶子。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/SeedCriteria.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SeedCriteria.h) | 判定条件的抽象接口，只有 `evaluate()` 和 `reset()` 两个纯虚函数 |
| [src/TimeSeedCriteria.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TimeSeedCriteria.cc) | 按时长判定的叶子条件（`--seed-time`） |
| [src/ShareRatioSeedCriteria.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ShareRatioSeedCriteria.cc) | 按分享率判定的叶子条件（`--seed-ratio`） |
| [src/UnionSeedCriteria.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnionSeedCriteria.cc) | 条件的 OR 组合节点 |
| [src/SeedCheckCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SeedCheckCommand.cc) | 常驻命令，周期性驱动判定、命中后广播停机 |
| [src/BtSetup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc) | 按选项装配判定树并创建 `SeedCheckCommand` |
| [src/BtRuntime.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRuntime.h) | 种子级运行时状态，`halt_` 标志与历史上传量都在这里 |
| [src/BtPostDownloadHandler.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPostDownloadHandler.cc) | `.torrent` 文件任务的 post 处理器：解析种子并派生数据任务 |
| [src/RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc) | 任务容器：post 处理器链、`enableSeedOnly` 状态切换 |
| [src/RequestGroupMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc) | 收割 `numCommand` 归零的任务并触发后处理链 |
| [src/DefaultPieceStorage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc) | 最后一个 piece 落位时触发「进入做种」的钩子 |

## 4. 核心概念与源码讲解

### 4.1 SeedCriteria 家族：退出判定的抽象接口

#### 4.1.1 概念说明

做种何时结束，本质是一个「谓词」：周期性地问一句「可以停了吗？」，答案是布尔值。aria2 把这个谓词抽象成 `SeedCriteria` 接口，再用两个叶子实现覆盖最常见的两类诉求：

- **TimeSeedCriteria**：做种满 N 秒即满足（对应 `--seed-time=分钟`，支持小数）。
- **ShareRatioSeedCriteria**：分享率达到 R 即满足（对应 `--seed-ratio`，默认 1.0）。

接口只有两个函数：`evaluate()` 回答「满足了吗」，`reset()` 在做种开始时重置内部状态。`reset()` 的存在是因为时间条件是有状态的——计时应从「下载完成那一刻」起算，而不是从进程启动起算。

#### 4.1.2 核心流程

两个叶子条件的求值逻辑：

```
TimeSeedCriteria::evaluate():
    return (当前时刻 - watch_) >= duration_
    # watch_ 在 reset() 时被设为调用时刻

ShareRatioSeedCriteria::evaluate():
    completedLength = 已完成字节数
    若 completedLength == 0:  返回 true      # 零长度种子无意义，直接放行
    uploadLength = 历史上传量(启动时快照) + 本次会话上传量
    return ratio_ <= uploadLength / completedLength
```

分享率的数学定义：

\[
\text{share ratio} \;=\; \frac{\text{uploadLengthAtStartup} + \text{sessionUploadLength}}{\text{completedLength}}
\]

两个细节值得注意：

1. **分子的两段式**：`uploadLengthAtStartup` 是从 `.aria2` 控制文件恢复出来的历史累计上传量（在 [src/DefaultBtProgressInfoFile.cc:L315](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L315) 中载入），`sessionUploadLength` 是本次进程的上传量。两者相加，断点续传后做种贡献不清零。
2. **分母是已完成长度而非总长度**：因为该条件只在 `downloadFinished()` 之后才会被求值（见 4.3），此时两者相等。
3. 代码里用 `1.0 * uploadLength / completedLength` 把 `int64_t` 提升为浮点再相除，避免整数除法截断成 0。

#### 4.1.3 源码精读

先看接口定义——整个判定体系的契约只有这两个函数：

- [src/SeedCriteria.h:L42-L55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SeedCriteria.h#L42-L55)：`SeedCriteria` 纯虚接口。`evaluate()` 返回「条件是否满足」，`reset()` 用于重置状态。注释极简，但这两个函数的语义撑起了整个做种退出机制。

时间条件的实现：

- [src/TimeSeedCriteria.cc:L47-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TimeSeedCriteria.cc#L47-L52)：`reset()` 把 `watch_` 拨到当前墙钟，`evaluate()` 比较「现在 − watch_」是否已超过 `duration_`。`Timer::difference` 返回非负时长，所以当 `duration_` 为 0（即 `--seed-time=0`）时，`evaluate()` 在 `reset()` 之后第一次调用就返回 true——这就是「下载完成立即退出、不做种」的实现原理。

分享率条件的实现：

- [src/ShareRatioSeedCriteria.cc:L52-L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ShareRatioSeedCriteria.cc#L52-L62)：完整的判定函数。L54-57 是零长度特例；L58-60 把「启动快照 + 本次会话」两段上传量相加；L61 做浮点比较。注意 `reset()` 在 [L50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ShareRatioSeedCriteria.cc#L50) 是空实现——该条件无内部状态，每次求值都实时读取 `PieceStorage` 和 `DownloadContext::getNetStat()`。
- [src/ShareRatioSeedCriteria.cc:L64-L74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ShareRatioSeedCriteria.cc#L64-L74)：两个 setter 注入 `BtRuntime`（取历史上传量）与 `PieceStorage`（取完成长度）。这是典型的「构造时给参数、装配时补依赖」风格。

#### 4.1.4 代码实践

1. **实践目标**：验证「`--seed-time=0` 意味着下载完成即退出做种」这一从源码推出的结论。
2. **操作步骤**：
   - 准备一个种子文件（可用任一公开种子，或用 `transmission-create`/`mktorrent` 对本地文件生成）。
   - 运行 `./src/aria2c --seed-time=0 -d /tmp/seed-test your.torrent`。
   - 对照 [src/SeedCheckCommand.cc:L71-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SeedCheckCommand.cc#L71-L76) 理解：`downloadFinished()` 为真的那一轮，`reset()` 刚把 `watch_` 拨到现在，下一轮 `evaluate()` 立即满足。
3. **需要观察的现象**：下载完成后日志几乎立即出现 `Seeding is over.`（该文案定义于 [src/message.h:L135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L135)），随后进程退出。
4. **预期结果**：`--seed-time=0` 与不指定 `--seed-time` 的行为截然不同——前者立即退出，后者（默认 `--seed-ratio=1.0`）在无人下载时上传量恒为 0，分享率永远达不到 1.0，进程会一直做种。具体退出时刻待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ShareRatioSeedCriteria::reset()` 是空实现，而 `TimeSeedCriteria::reset()` 必须做事？

**答案**：时间条件是有状态的——它需要知道「从何时起算」，`reset()` 就是设定这个起点的动作。分享率条件是无状态的——它每次求值都实时读取当前的完成长度与上传量，不存在需要重置的内部计时器。

**练习 2**：假设某种子总长 100 MB，你已做过种、控制文件记录历史上传量 60 MB；本次进程断点续传后又上传了 50 MB，`--seed-ratio=1.5`。此刻条件满足吗？

**答案**：不满足。分子 = 60 MB + 50 MB = 110 MB，分母 = 100 MB，实际分享率 = 1.1 < 1.5。若再上传 40 MB 使分子达到 150 MB，分享率 = 1.5，条件即满足。这个练习的关键是记住分子是「历史 + 本次」两段之和——控制文件让上传贡献跨进程累计，不会因为重启 aria2 而清零。

---

### 4.2 UnionSeedCriteria 组合求值与 BtSetup 装配

#### 4.2.1 概念说明

单个条件不够用：用户可能同时给出 `--seed-time=30 --seed-ratio=2.0`，官方帮助文本明确此时语义是「**任一条件满足即结束做种**」（OR 而非 AND）。`UnionSeedCriteria` 就是这个 OR 节点：它自己也是一个 `SeedCriteria`（组合模式），内部持有一组子条件，`evaluate()` 逐个求值、短路返回。

谁来决定「装配哪些子条件」？是 `BtSetup::setup`——它在 BT 任务启动时读取选项，按规则拼出判定树：

| 选项取值 | 装配结果 |
|----------|----------|
| `--seed-time` 未指定（无默认值，即 undefined） | 不装配 `TimeSeedCriteria` |
| `--seed-time=0` 或任意值（只要显式给出） | 装配 `TimeSeedCriteria(分钟 × 60 秒，截断取整)` |
| `--seed-ratio > 0`（默认 1.0） | 装配 `ShareRatioSeedCriteria(该值)` |
| `--seed-ratio=0.0` | 不装配（帮助文本：「设 0.0 表示无视分享率、无限做种」） |
| 两个条件都没有 | 不创建 `SeedCheckCommand`，无判定循环 |

#### 4.2.2 核心流程

```
BtSetup::setup（每个非 metadataGetMode 的种子任务执行一次）:
    unionCri = new UnionSeedCriteria
    若 option 定义了 seed-time:
        unionCri.add(TimeSeedCriteria(seed-time 分钟 × 60))
    ratio = option 的 seed-ratio
    若 ratio > 0.0:
        cri = ShareRatioSeedCriteria(ratio, downloadContext)
        cri.setPieceStorage(...)   cri.setBtRuntime(...)
        unionCri.add(cri)
    若 unionCri 非空:
        创建 SeedCheckCommand(unionCri)，注入 PieceStorage 与 BtRuntime
```

`UnionSeedCriteria::evaluate()` 的求值顺序就是 `addSeedCriteria` 的调用顺序：**先时间、后分享率**，且短路——时间条件一旦满足，分享率条件本轮不会被求值。

#### 4.2.3 源码精读

- [src/UnionSeedCriteria.cc:L51-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnionSeedCriteria.cc#L51-L59)：OR 语义的核心实现——遍历子条件，任何一个 `evaluate()` 返回 true 就立即返回 true（短路）；全部不满足才返回 false。仅 8 行，却定义了多条件做种的全部组合语义。
- [src/UnionSeedCriteria.cc:L44-L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnionSeedCriteria.cc#L44-L49) 与 [src/UnionSeedCriteria.cc:L61-L64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnionSeedCriteria.cc#L61-L64)：`reset()` 递归重置全部子条件；`addSeedCriteria()` 把子条件追加到 vector 尾部——追加顺序即求值顺序。
- [src/BtSetup.cc:L157-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L157-L183)：判定树的装配现场。L159 判断 `option->defined(PREF_SEED_TIME)`——`seed-time` 在选项注册表中是 `NO_DEFAULT_VALUE`，未显式给出时 `defined()` 为假，所以默认**没有**时间条件；L161-162 把分钟转秒（`static_cast<int>(分钟 × 60)`，0.5 分钟 = 30 秒）；L165-174 处理分享率，`ratio > 0.0` 才装配；L176-182 只有判定树非空才创建 `SeedCheckCommand`。注意整个块被 `if (!metadataGetMode)` 包裹（L157）——magnet 的元数据获取任务不做种。
- [src/usage_text.h:L316-L327](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h#L316-L327)：`--seed-time` 与 `--seed-ratio` 的帮助文本。L325-327 明确写出「两者同时指定时，**至少一个**条件满足即结束」——这正是 `UnionSeedCriteria` 短路 OR 的用户侧文档。
- [src/OptionHandlerFactory.cc:L1870-L1887](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1870-L1887)：两个选项的注册处。`seed-time` 无默认值、`seed-ratio` 默认 `"1.0"`、下界都是 0.0——对照 u1-l5 讲过的 `OptionHandler` 校验体系，传负数会在入口被拒绝。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：用真实运行观察多条件并存时的求值顺序与短路行为，确认「实际退出时刻由哪个条件决定」。
2. **操作步骤**：
   - 在 [src/UnionSeedCriteria.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnionSeedCriteria.cc) 的 `evaluate()` 循环体内临时加一行日志（**示例代码**，非项目原有，实验后请还原）：

     ```cpp
     // 示例代码：观察每个子条件的求值结果
     bool r = c->evaluate();
     A2_LOG_INFO(fmt("UnionSeedCriteria: criterion#%zu -> %s", i++, r ? "true" : "false"));
     if (r) { return true; }
     ```

     （需要在文件头部补 `#include "Logger.h"`、`#include "LogFactory.h"`、`#include "fmt.h"`，并声明计数变量 `size_t i = 0;`。）重新编译：`make -j$(nproc)`。
   - 场景 A：`./src/aria2c --seed-time=0 --seed-ratio=1.0 -l log.txt -d /tmp/s1 your.torrent`。
   - 场景 B：`./src/aria2c --seed-time=0.05 --seed-ratio=1.0 -l log.txt -d /tmp/s2 your.torrent`（0.05 分钟 = 3 秒，秒数经 `static_cast<int>` 截断）。
3. **需要观察的现象**：
   - 场景 A 中，下载完成瞬间即可看到子条件 0（Time）求值为 true 的日志，且**同一条日志之后不再出现子条件 1（Ratio）的求值记录**——短路生效。
   - 场景 B 中，做种期间每轮循环都会打出「criterion#0 -> false, criterion#1 -> false」成对出现的日志（无人下载时 Ratio 恒为 false），3 秒后 criterion#0 变 true、进程退出。
4. **预期结果**：退出由 `UnionSeedCriteria` 中**第一个满足的子条件**决定；装配顺序固定为「时间在前、分享率在后」，所以时间条件满足时分享率条件根本不会被求值。若只有 `--seed-ratio=1.0`（不设 seed-time）且无人来下载，上传量恒为 0，任务将无限做种——这一点待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`--seed-time=0.5` 实际换算成多少秒？为什么不是精确的 30.000 秒边界也基本无影响？

**答案**：`static_cast<int>(0.5 × 60) = 30` 秒。判定发生在 `SeedCheckCommand` 的执行轮次上（realtime 命令，每轮主循环都会执行），粒度是主循环周期而非毫秒级定时器，所以边界附近有一两个循环周期的误差是正常且无影响的。

**练习 2**：帮助文本说 `--seed-ratio=0.0` 表示「无视分享率、无限做种」。源码里「无限」是如何实现的？

**答案**：不是让某个条件永远返回 false，而是 [src/BtSetup.cc:L166](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L166) 的 `if (ratio > 0.0)` 直接**不装配** `ShareRatioSeedCriteria`。若用户同时也没给 `--seed-time`，判定树为空、`SeedCheckCommand` 不创建，做种便没有退出条件（只能靠 Ctrl-C 或 `--bt-stop-timeout` 等外部机制停止）。

**练习 3**：如果想要「时间 AND 比例都满足才退出」的语义，最少要改哪里？

**答案**：把 [src/UnionSeedCriteria.cc:L53-L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnionSeedCriteria.cc#L53-L57) 循环里的「遇 true 立即返回 true」改为「遇 false 立即返回 false、循环结束返回 true」即可（即把 OR 短路换成 AND 短路）。组合模式的好处正在于此：语义变化只动组合节点，叶子条件完全不用改。

---

### 4.3 SeedCheckCommand：周期裁定与停机广播

#### 4.3.1 概念说明

判定树是被动对象，需要有东西**周期性地去问它**。`SeedCheckCommand` 就是这个驱动者：一个随 BT 任务一起创建、常驻到做种结束的 `Command`。它做三件事：

1. **等待下载完成**：完成之前不启动判定（否则分享率的分母还没满、时间的起点也不对）。
2. **启动判定**：检测到 `downloadFinished()` 的那一轮调用 `reset()`——这就是「计时从完成时刻起算」的落点。
3. **命中即广播停机**：条件满足时设置 `BtRuntime::halt_` 标志。这个标志是**广播式**的——所有 BT 命令在各自的执行入口检查 `isHalt()`，看到即自行退出，于是整个种子的全部命令有序退场。

#### 4.3.2 核心流程

```
SeedCheckCommand::execute()   # Realtime 命令，每轮主循环执行:
    若 btRuntime_->isHalt():  return true          # 已停机，自我销毁
    若无判定树:               return false         # (防御分支)
    若尚未开始检查:
        若 pieceStorage_->downloadFinished():
            checkStarted_ = true
            seedCriteria_->reset()                 # 计时/状态起点 = 此刻
    若已开始检查:
        若 seedCriteria_->evaluate():
            日志 "Seeding is over."
            btRuntime_->setHalt(true)              # 广播停机
    e_->addCommand(this)                           # 重排自己
    return false
```

停机标志的涟漪：`setHalt(true)` 之后，`PeerInteractionCommand`（每个 peer 会话一员）、`PeerChokeCommand`、`ActivePeerConnectionCommand`、`TrackerWatcherCommand`、`DHTGetPeersCommand` 等种子级命令在下一次执行时各自检查 `isHalt()` 并返回 true 销毁；命令析构使 `RequestGroup::numCommand` 递减，归零后任务被收割（见 4.4）。

还有一个容易忽略的锚点：`SeedCheckCommand` 构造时 `increaseNumCommand()`、析构时 `decreaseNumCommand()`——正是它（和其他常驻命令）把 `numCommand` 顶在 0 以上，任务在做种期间才不会被引擎当作已结束而收割。

#### 4.3.3 源码精读

- [src/SeedCheckCommand.cc:L48-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SeedCheckCommand.cc#L48-L61)：构造函数 `setStatusRealtime()` 把自己标记为实时命令（对照 u2-l3：realtime 命令每轮主循环都会被调度，不受每秒一次的 INACTIVE 唤醒闸门约束）；`increaseNumCommand()` 锁定任务的存活。析构函数对称地 `decreaseNumCommand()`。
- [src/SeedCheckCommand.cc:L63-L85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SeedCheckCommand.cc#L63-L85)：`execute()` 全貌。L65-67 停机自查（防止其他停机源已触发后本命令还残留）；L71-76 是「完成检测 + reset」闸门，`checkStarted_` 保证 reset 只做一次；L77-81 命中后打印 `MSG_SEEDING_END` 并 `setHalt(true)`；L83-84 经典的自我重排（返回 false 的命令必须已把自己加回队列，对照 u2-l1 的约定）。
- [src/BtRuntime.h:L67-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRuntime.h#L67-L69)：`isHalt()`/`setHalt()`——一个普通 bool 成员（[L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRuntime.h#L45) `halt_`），没有锁、没有条件变量：因为 aria2 是单线程事件循环（u2-l1），跨命令共享状态只需普通标志位。
- [src/PeerInteractionCommand.cc:L410-L413](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L410-L413)：peer 会话命令消费停机标志的典型位置——`exitBeforeExecute()` 返回 true 即销毁。类似的检查散布在 `PeerChokeCommand`、`ActivePeerConnectionCommand`、`TrackerWatcherCommand`、`DHTGetPeersCommand` 等命令中，构成「一处置位、处处自查」的优雅停机网。
- [src/BtStopDownloadCommand.cc:L59-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtStopDownloadCommand.cc#L59-L72)：另一条独立的停机路径——`--bt-stop-timeout`。它是「下载速度归零超过 N 秒就强制停」的兜底命令，与做种判定无关，但同样以 `btRuntime_->isHalt()` 或 `setForceHaltRequested` 收尾，可对照理解「多停机源共用一个标志」的设计。

#### 4.3.4 代码实践

1. **实践目标**：验证「`reset()` 的调用时刻 = 下载完成时刻」，即做种计时不含下载阶段。
2. **操作步骤**：
   - 在 [src/SeedCheckCommand.cc:L74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SeedCheckCommand.cc#L74) 的 `seedCriteria_->reset();` 前后各加一行日志（**示例代码**，实验后还原）：打印「seed criteria reset」与当前墙钟时间。
   - 用 `--seed-time=0.1`（6 秒）下载任一种子：`./src/aria2c --seed-time=0.1 -l log.txt -d /tmp/s3 your.torrent`。
3. **需要观察的现象**：日志中「reset」出现在**下载完成**（`Download complete` 类日志）之后，而不是进程启动时；此后约 6 秒出现 `Seeding is over.`。
4. **预期结果**：从下载完成到退出之间的间隔 ≈ `--seed-time` 换算的秒数，与下载耗时无关。具体数值待本地验证（realtime 命令的调度粒度会带来亚秒级误差）。

#### 4.3.5 小练习与答案

**练习 1**：`SeedCheckCommand` 为什么必须在构造时 `increaseNumCommand()`？如果漏掉这一行会怎样？

**答案**：`numCommand` 是任务存活的判据（u3-l1）。做种期间下载类命令大多已退出，若无 `SeedCheckCommand`、`PeerInteractionCommand` 等常驻命令把计数顶住，`numCommand` 提前归零，`RequestGroupMan` 会收割任务、触发后处理链，做种根本无法进行。

**练习 2**：`SeedCheckCommand::execute()` 里 L65-67 的停机自查为什么放在最前面？它处理的是哪种场景？

**答案**：处理「停机由别人触发」的场景——例如 Ctrl-C、RPC `aria2.remove`、或 `--bt-stop-timeout` 兜底。这些停机源先把 `halt_` 置位，若不做自查，本命令会带着已失效的判定树继续空转，阻止任务收割。放在最前面保证一旦停机就最快销毁。

---

### 4.4 RequestGroup 状态流转与 BtPostDownloadHandler 后处理链

#### 4.4.1 概念说明

本讲标题中的「从下载到做种的状态切换」实际包含**两条不同的链路**，初学者极易混淆：

**链路 A——`.torrent` 文件任务 → BT 数据任务**：当 aria2 以 `aria2c file.torrent` 启动时，第一个 `RequestGroup` 的「下载内容」就是 .torrent 文件本身（常驻内存）。它完成后，`BtPostDownloadHandler` 解析种子、创建真正承载数据的 `RequestGroup`，经 `followedBy`/`following` 链交接（对照 u3-l1、u3-l3）。这是**任务派生**，不是做种。

**链路 B——BT 数据任务完成 → 做种态**：最后一个 piece 落位时，`DefaultPieceStorage::completePiece` 触发 `on-bt-download-complete` 钩子，并调用 `RequestGroup::enableSeedOnly()`。此后任务不再产生下载命令，但常驻命令（含 `SeedCheckCommand`）仍在跑——任务进入做种态。做种结束时（4.3 的停机广播）`numCommand` 归零，任务被收割，控制文件按条件删除。

`enableSeedOnly` 配合 `--bt-detach-seed-only` 选项（默认 `false`）：开启后，进入做种态的任务会从「并发活跃下载数」（`-j` 的计数）中摘除，把名额让给等待队列中的下一个任务——做种任务本身继续留在引擎里上传。

#### 4.4.2 核心流程

两条链路的全景（省略错误分支）：

```
链路 A（任务派生）:
  .torrent 任务 numCommand == 0
    → RequestGroupMan::processStoppedRequestGroup 收割
    → downloadFinished() 成立
    → reportDownloadFinished / 删或存控制文件
    → postDownloadProcessing
    → BtPostDownloadHandler::getNextRequestGroups
        · 内存下载: 从 BencodeDiskWriter 直接取 bencode 树
        · 磁盘下载: 读文件 + ValueBaseBencodeParser 解析
        · createRequestGroupForBitTorrent 生成数据任务
        · followedBy / following 建链
    → insertReservedGroup(0, newRgs)  # 插到等待队列头，尽快启动

链路 B（进入做种 → 退出）:
  最后一个 piece completePiece
    → downloadFinished() 成立
    → on-bt-download-complete 钩子 + 事件通知
    → enableSeedOnly()   # --bt-detach-seed-only=true 时摘出 -j 计数
    → （SeedCheckCommand 周期判定，见 4.3）
    → setHalt(true) → 各常驻命令自查退出 → numCommand == 0
    → 收割: allDownloadFinished() 且未设 force-save → removeControlFile
```

#### 4.4.3 源码精读

**链路 A：**

- [src/BtPostDownloadHandler.cc:L59-L63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPostDownloadHandler.cc#L59-L63)：构造函数给自己挂上「内容类型/扩展名」判据（`ContentTypeRequestGroupCriteria`），`canHandle()` 靠它判断一个完成的任务是不是 .torrent 下载。
- [src/BtPostDownloadHandler.cc:L65-L101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPostDownloadHandler.cc#L65-L101)：取回种子内容的两条路径——L72-81 内存下载（`--follow-torrent=mem` 或 magnet 派生场景）直接向 `BencodeDiskWriter` 要解析结果；L82-97 磁盘下载则打开文件、整体读入、用 `ValueBaseBencodeParser` 现场解析（对照 u6-l1 的 bencode 体系）。两条路径殊途同归于一个 `ValueBase` 树。
- [src/BtPostDownloadHandler.cc:L102-L125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPostDownloadHandler.cc#L102-L125)：派生数据任务。L103-105 调用 u3-l3 讲过的 `createRequestGroupForBitTorrent` 工厂；L106-109 建立 `followedBy`/`following` 双向链；L116-123 是 RPC 常驻模式（`--rpc-save-upload-metadata` 相关的 `keepRunning` 与 `--pause-metadata` 组合）下把新任务标记为暂停的特殊处理；L125 把新任务交给调用方。
- [src/RequestGroup.cc:L1064-L1082](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1064-L1082)：`postDownloadProcessing`——遍历 `postDownloadHandlers_`，第一个 `canHandle` 的处理器执行 `getNextRequestGroups`。而处理器列表的注册在 [src/RequestGroup.cc:L1100-L1116](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1100-L1116)：只要 `--follow-torrent` 不是 `false`，`BtPostDownloadHandler` 就在列（单例，经 [src/download_handlers.cc:L113-L119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_handlers.cc#L113-L119) 惰性创建）。
- [src/RequestGroupMan.cc:L379-L398](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L379-L398)：收割函数 `ProcessStoppedRequestGroup::operator()` 的完成分支——L381-382 应用远端时间戳并上报完成；L383-390 `allDownloadFinished()` 成立且未开 `--force-save` 则**删除控制文件**、保存签名，否则保存控制文件（做种中断后还能续）；L391-398 调用链路 A 的后处理，并把派生任务 `insertReservedGroup(0, ...)` 插到等待队列**最前面**。

**链路 B：**

- [src/DefaultPieceStorage.cc:L459-L511](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L459-L511)：`completePiece` 的完成分支。L468-470 幂等保护（已全完成就直接返回）；L474 `downloadFinished()` 成立后走收尾：L483 打印完成日志、L499-504 触发 `--on-bt-download-complete` 钩子与 `EVENT_ON_BT_DOWNLOAD_COMPLETE` 事件通知（对照 u9-l6 的 Notifier 体系）、L506 调用 `enableSeedOnly()`——**这就是「从下载到做种」状态切换的精确落点**。
- [src/RequestGroup.cc:L1325-L1337](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1325-L1337)：`enableSeedOnly` 全貌。L1327 的前置检查——已开启过、或 `--bt-detach-seed-only` 为 false 都直接返回（该选项默认 false，见 [src/OptionHandlerFactory.cc:L1464-L1465](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1464-L1465)）；L1334-1335 摘出活跃计数并触发 `requestQueueCheck`（对照 u3-l1：这会让等待队列中的下一个任务顶上腾出的 `-j` 名额）。与之配套，收割函数在 [src/RequestGroupMan.cc:L357-L359](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L357-L359) 检查 `isSeedOnlyEnabled()`（[src/RequestGroup.h:L509](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.h#L509)），避免对已摘出的任务重复 `decreaseNumActive`。
- [src/BtFileAllocationEntry.cc:L94-L109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtFileAllocationEntry.cc#L94-L109)：`enableSeedOnly` 的第二个调用点——启动时就发现文件已下载完（断点续传做种场景），跳过下载命令的创建，直接切入做种态。
- [src/RequestGroup.cc:L1339-L1348](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1339-L1348)：`isSeeder()`——「有完整 metadata 且已下载完成」即 seeder。这个标志影响 tracker announce 的 `left=0` 参数与 peer 间的兴趣关系（对照 u6-l4、u6-l7）。

#### 4.4.4 代码实践

1. **实践目标**：亲眼区分链路 A 与链路 B——看懂一次 `aria2c file.torrent` 运行中任务的派生与收割。
2. **操作步骤**：
   - 运行 `./src/aria2c --seed-time=0 -l log.txt --log-level=info -d /tmp/s4 your.torrent`。
   - 在日志中按时间顺序检索三类行：①「Generating RequestGroups for Torrent file」（来自 [src/BtPostDownloadHandler.cc:L69-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPostDownloadHandler.cc#L69-L70)）——链路 A 发生；②「Download complete」（来自 [src/DefaultPieceStorage.cc:L483](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L483) 的 `MSG_DOWNLOAD_COMPLETED`）——数据任务完成；③「Seeding is over.」——链路 B 收官。
   - 统计 GID 的变化：日志里会出现两个不同的 GID，前者（torrent 任务）完成后由后者（数据任务）接棒。
3. **需要观察的现象**：三个事件严格有序：①在最前（几乎在启动后立刻，因为 .torrent 转瞬即完成）、②在中、③在尾。
4. **预期结果**：①与②之间的间隔极短，②与③之间的间隔≈0 秒（`--seed-time=0`）。若把 `--seed-time` 换成 60，③会推迟约 1 分钟，期间用 `tail -f log.txt` 能看到上传活动。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`aria2c file.torrent` 启动后，第一个被收割的 `RequestGroup` 下载的「文件」是什么？它落盘了吗？

**答案**：下载的是 .torrent 元数据本身。默认 `--follow-torrent=true` 时它以内存下载（`inMemoryDownload()` 为真）承载，不落盘，内容经 `BencodeDiskWriter` 以 bencode 树的形式留在内存里，供 `BtPostDownloadHandler` 直接取用。

**练习 2**：做种结束、任务收割时，`.aria2` 控制文件什么时候删、什么时候留？

**答案**：见 [src/RequestGroupMan.cc:L383-L390](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L383-L390)：`allDownloadFinished()`（全部文件完成，而非仅选中文件完成，对照 u5-l2）且未开 `--force-save` 时删除；否则（如 `--select-file` 部分下载、或开了 force-save）保存，供下次续传。

**练习 3**：`--bt-detach-seed-only=true` 时，RPC 的 `aria2.tellActive` 会把做种任务算作活跃吗？

**答案**：会。该选项只影响本地 `-j` 并发计数的口径（`enableSeedOnly` 摘出 `numActive`），官方手册明确「RPC 方法中做种项仍被认作活跃下载」——RPC 侧有自己的过滤口径，不走 `seedOnly_` 标志。

---

## 5. 综合实践

把本讲四个模块串成一个完整实验：**给同一个种子做三次「退出行为」对照实验，并用日志证据链解释每一次退出的决策者**。

1. **准备**：
   - 选一个总大小较小的公开种子（或自制种子并另起一个 aria2 实例当 peer，模拟真实上传）。
   - 按 4.2.4 的方法在 `UnionSeedCriteria::evaluate` 与 `SeedCheckCommand::execute` 各加日志（标注：示例代码，实验后还原），重新编译。
2. **三次运行**：
   - 运行 1：`./src/aria2c --seed-time=0 -l r1.txt -d /tmp/e1 your.torrent` → 预期：Time 条件立即满足，`Seeding is over.` 紧跟 `Download complete`。
   - 运行 2：`./src/aria2c --seed-ratio=0.0001 -l r2.txt -d /tmp/e2 your.torrent` → 预期：只要有任何一丁点上传（哪怕几 KB），Ratio 条件即满足；完全无 peer 时则永远做种（可用 Ctrl-C 结束观察现象）。
   - 运行 3：`./src/aria2c --seed-time=1 --seed-ratio=0.0001 -l r3.txt -d /tmp/e3 your.torrent` → 预期：与运行 2 相比多了 1 分钟的兜底；若期间有 peer 下载导致 Ratio 先满足，日志会显示 criterion#1（Ratio）先变 true——证明「谁先满足谁决策」与装配顺序无关。
3. **产出**：写一份对照笔记，包含三个日志文件中以下证据行的截图/摘录与时间差：`Download complete`、每次 criterion 求值行、`Seeding is over.`，并回答：三次运行中，实际退出时刻分别由哪个条件决定？短路求值在哪些日志行得到体现？
4. **加分项**：把 `--bt-detach-seed-only=true` 加到运行 3，配合 `-j1` 与一个排队的 HTTP 下载（用 `-i` 输入文件），验证数据任务进入做种态后 HTTP 任务是否立即获得名额启动（对应 `enableSeedOnly` → `requestQueueCheck` 链路）。待本地验证。

## 6. 本讲小结

- 做种退出判定是一棵**组合模式**的判定树：`SeedCriteria` 接口（`evaluate`/`reset`）+ 叶子（`TimeSeedCriteria`、`ShareRatioSeedCriteria`）+ OR 组合节点（`UnionSeedCriteria`，短路求值）。
- 分享率 = \( \frac{\text{历史上传量（控制文件恢复）} + \text{本次会话上传量}}{\text{已完成长度}} \)，跨进程累计贡献；`--seed-ratio=0.0` 的「无限做种」靠的是**不装配**该条件。
- `BtSetup` 按选项装配判定树：`seed-time` 无默认值（不指定即无时间条件），装配顺序固定「时间在前、分享率在后」，因此求值顺序也固定；树为空则不创建 `SeedCheckCommand`。
- `SeedCheckCommand` 是常驻 realtime 命令：完成前空转，完成时刻调 `reset()`（计时起点=完成时刻），此后每轮裁定；命中即 `BtRuntime::setHalt(true)` 广播停机，全套 BT 命令自查 `isHalt()` 优雅退场，`numCommand` 归零收尾。
- 「下载→做种」的切换有两条易混链路：`.torrent` 文件任务完成后经 `BtPostDownloadHandler` **派生**数据任务（followedBy/following 链，插等待队列头）；数据任务最后一个 piece 完成时经 `completePiece → enableSeedOnly` 进入**做种态**，`--bt-detach-seed-only`（默认 false）可把它摘出 `-j` 并发计数。
- 做种结束后的收割路径中，`allDownloadFinished()` 且未开 `--force-save` 才删除 `.aria2` 控制文件，否则保留供续传。

## 7. 下一步学习建议

- **下一讲 u6-l11（会话恢复与 .aria2 控制文件）**：本讲反复出现的 `uploadLengthAtStartup` 正是 `DefaultBtProgressInfoFile` 从控制文件载入的，做种中断后控制文件的去留也指向同一子系统——接下来就该打开 `.aria2` 文件格式一探究竟。
- **回看巩固**：u6-l4 的 `DefaultBtInteractive` 中 keepalive、choke 决策等节律器与本讲的 `SeedCheckCommand` 同属「常驻命令 + 周期裁定」家族，对照阅读能加深对 u2-l3 命令生命周期模型的理解。
- **延伸阅读**：若你对「停机的多种触发源」感兴趣，可顺读 `BtStopDownloadCommand`（`--bt-stop-timeout` 兜底）与 u2-l1 讲过的 `globalHaltRequested` 状态机（Ctrl-C 两段式停机），体会 aria2 如何用统一的 halt 标志汇聚所有停机路径。
