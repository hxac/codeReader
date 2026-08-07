# 特权降级与守护进程化

## 1. 本讲目标

本讲聚焦于 `src/util.rs` 这个不到 90 行的小文件，回答三个问题：

1. 一个需要 root 才能启动的网络守护进程，启动后如何**放弃** root 权限，把自己降级为 `nobody`？
2. 它又如何把自己从终端里“剥离”，变成一个**后台守护进程**（daemon）？
3. 这两件事在 `main.rs` 里的**调用时机**是什么？为什么顺序不能颠倒？

学完后，读者应当能够：

- 说清 `daemonize()` 的“双 fork + `setsid`”经典套路，以及每一步解决什么问题。
- 说清 `drop_privileges()` 里 `getpwnam → chroot → umask → chdir → setgid → setuid` 的先后顺序及其安全动机。
- 读懂 `DaemonizeError` 错误类型，以及 `main.rs` 对每种失败的处理方式。

本讲承接 [u1-l4](u1-l4-main-entry.md)。在 u1-l4 中我们已经看到 `main.rs` 的整体生命周期，但当时把 `drop_privileges` 与 `daemonize` 当成两个黑盒留到了这里。本讲就是打开这两个黑盒。

## 2. 前置知识

本讲需要一点 Unix 系统编程的直觉。初学者不用慌，下面用最朴素的语言铺垫。

### 2.1 进程、会话与控制终端

- **进程（process）**：一个正在运行的程序，有一个唯一的进程号 PID。
- **进程组 / 会话（session）**：若干进程可以组成“进程组”，若干进程组再组成“会话”。每个会话最多关联一个**控制终端**（就是你敲命令的那个终端窗口）。
- 麻烦在于：当一个会话的“会话首领（session leader）”所在终端被关闭或断开时，内核会向该会话所有进程发送 `SIGHUP`，默认行为是**把进程杀掉**。这就是为什么你直接在终端跑一个服务、关掉终端它就死了。

**守护进程化（daemonize）** 的目标，就是让进程脱离任何控制终端、脱离启动它的 shell，这样终端关掉它还能活着。`setsid()` 就是为这个设计的：它让调用者成为一个**新会话**的首领，并且不再有控制终端。

### 2.2 uid、gid 与 root

- Linux 上每个进程有一个真实/有效 **uid（用户号）** 和 **gid（组号）**。
- **root 的 uid 是 0**。uid 为 0 的进程几乎不受权限检查约束——可以读写任意文件、调用 `chroot`、调用 `setuid` 切到任意用户等。
- `nobody` 是一个几乎所有发行版都自带的“最小权限”占位用户，它不属于任何敏感组、对系统文件几乎没有权限。把一个服务降级成 `nobody` 是常见的“最小权限”实践：**只在确实需要 root 的时候持有 root，用完立刻丢掉**。

### 2.3 两个关键约束（本讲会反复用到）

1. **`setuid` 是“单向降级”**：一旦你从 root（uid=0）`setuid` 到非 root，就再也升不回去了——后续 `setuid`/`setgid` 只能向更低权限走。所以凡是需要 root 才能做的事（`chroot`、`setgid`、`setuid` 本身），都必须在 `setuid` **之前**完成。
2. **`chroot` 改变“根目录视图”**：`chroot("/tmp")` 之后，进程看到的 `/` 其实是原来的 `/tmp`，再也访问不到 `/etc/passwd` 等文件。所以任何依赖 `/etc/passwd` 的查询（如 `getpwnam`）必须在 `chroot` **之前**完成。

把这两条约束记牢，`drop_privileges` 里那段看似随意的步骤顺序就会变得“非如此不可”。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/util.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/util.rs) | 全部内容：`DaemonizeError` 错误类型、`daemonize()`、`drop_privileges()` 两个函数。 |
| [src/main.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs) | 调用方：解析 `--disable-drop-privileges` / `-f` 两个开关，并在绑定 UAPI、创建 TUN 之后、起工作线程之前调用这两个函数。 |

本讲只引用这两个文件。它们都属于 [u1-l3](u1-l3-directory-map.md) 建立的 `src/` 模块地图里的“工具/入口”层，与协议核心 `wireguard/` 无耦合。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 守护进程化 `daemonize`**：双 fork + `setsid`。
- **4.2 特权降级 `drop_privileges`**：`chroot` + `setgid`/`setuid` 的有序降权。
- **4.3 错误类型 `DaemonizeError` 与 `main.rs` 的调用时机**。

### 4.1 守护进程化 daemonize

#### 4.1.1 概念说明

“守护进程化”要解决的问题是：让 `wireguard-rs` 这个进程在用户关闭终端、退出登录后仍能继续运行，并且不与任何终端绑定。

经典的 Unix 做法（Stevens 在《APUE》里给出的套路）由三步组成：

1. **第一次 fork**：父进程立刻退出，子进程继续。这一步保证子进程**不是进程组组长**——这是 `setsid` 成功的前提。
2. **`setsid()`**：让这个子进程成为**新会话的首领**兼新进程组组长，并脱离任何控制终端。
3. **第二次 fork**：再 fork 一次、父（即第一次的子）退出。这一步保证最终的进程**不再是会话首领**，从而“依照 POSIX 规则，永远无法重新获得控制终端”。这是一个加固步骤。

这个套路被称作 **double-fork（双 fork）**。

#### 4.1.2 核心流程

`daemonize` 的执行流程可以画成：

```text
原始进程 (会话 A, 受终端控制)
   │ fork()            ← fork_and_exit() 第一次
   ├─→ 父进程: exit(0) 立即退出
   └─→ 子进程 (仍属会话 A, 但不是组长)
          │ setsid()    ← 创建新会话 B, 自己当首领
          │ fork()      ← fork_and_exit() 第二次
          ├─→ 子进程(第一次的): exit(0) 退出
          └─→ 孙进程 (属会话 B, 不是首领, 无控制终端)  ← 真正的 daemon
```

注意中间的辅助函数 `fork_and_exit`：它把“fork 一次，父死子活”这个模式抽出来复用了两次。它的分支逻辑只看 `fork()` 的返回值：

- 返回值 `< 0`：fork 失败 → 返回错误。
- 返回值 `== 0`：当前在**子进程**里 → 返回 `Ok(())`，继续往下走。
- 返回值 `> 0`：当前在**父进程**里，返回值是子进程的 PID → 直接 `exit(0)` 自杀。

#### 4.1.3 源码精读

先看被复用两次的 `fork_and_exit`：

[fork_and_exit：fork 之后父进程退出、子进程继续](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/util.rs#L31-L38)

```rust
fn fork_and_exit() -> Result<(), DaemonizeError> {
    let pid = unsafe { fork() };
    match pid.cmp(&0) {
        Ordering::Less => Err(DaemonizeError::Fork),   // fork 失败
        Ordering::Equal => Ok(()),                      // 子进程：继续
        Ordering::Greater => exit(0),                   // 父进程：退出
    }
}
```

`fork()` 是 libc 的原始系统调用，返回 `i32`：负数表示失败，0 表示在子进程中，正数（子进程 PID）表示在父进程中。这里用 `pid.cmp(&0)` 一次性把三种情况区分开。

再看 `daemonize` 本体：

[daemonize：第一次 fork → setsid → 第二次 fork](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/util.rs#L40-L51)

```rust
pub fn daemonize() -> Result<(), DaemonizeError> {
    // fork from the original parent
    fork_and_exit()?;                       // 第 1 步

    // avoid killing the child when this parent dies
    if unsafe { setsid() } < 0 {            // 第 2 步
        return Err(DaemonizeError::SetSession);
    }

    // fork again to create orphan
    fork_and_exit()                         // 第 3 步
}
```

三步与前文流程一一对应。`setsid()` 返回负数表示失败（最常见的失败原因是调用者已经是进程组组长，因此必须先 fork 一次）。第二次 `fork_and_exit` 的返回值直接作为函数结果返回：到了孙进程它返回 `Ok(())`。

> 术语提示：代码注释里的 “create orphan（创造孤儿）”指的是，第二次 fork 后孙进程的父进程（第一次的子进程）随即 `exit`，孙进程被 init（PID 1）收养，成为“孤儿”——这正是脱离启动 shell 的标志。

#### 4.1.4 代码实践

**实践目标**：用最朴素的方式，亲眼看到“父进程退出、孙进程变成孤儿并被 init 收养”这件事。

**操作步骤**（这只是一个 shell 观察实验，**不修改源码**）：

1. 准备一段独立的小程序 `dfork_demo.rs`（**示例代码**，不是本项目源码）：

   ```rust
   fn main() {
       println!("原始进程 pid={}, ppid={}", std::process::id(), parent_pid());
       let pid = unsafe { libc::fork() };
       if pid > 0 { std::process::exit(0); }      // 父进程立刻退出
       unsafe { libc::setsid(); }                  // 子进程脱离会话
       let pid2 = unsafe { libc::fork() };
       if pid2 > 0 { std::process::exit(0); }      // 子进程退出
       // 孙进程
       println!("孙进程 pid={}, ppid={}", std::process::id(), parent_pid());
       std::thread::sleep(std::time::Duration::from_secs(30));
   }

   fn parent_pid() -> u32 { unsafe { libc::getppid() as u32 } }
   ```

2. 在 `Cargo.toml` 的临时 demo 项目里加 `libc = "*"`，`cargo run` 跑起来。
3. 在孙进程 sleep 的 30 秒内，另开一个终端用 `ps -o pid,ppid,sess,cmd -p <孙进程pid>` 观察它。

**需要观察的现象**：

- 第一行打印的原始进程 pid 在程序结束后会消失（父进程早已 `exit`）。
- 孙进程的 `ppid` 变成 **1（init）**，说明它已经被收养为孤儿。
- 孙进程的 `sess`（会话号）与原终端的会话号不同，说明它脱离了控制终端。

**预期结果 / 待本地验证**：孙进程 `ppid == 1`、会话独立，关闭原终端后孙进程仍存活。由于本环境未必允许任意 fork 实验，若无法运行请标注「待本地验证」，并改为**源码阅读型实践**：对照 `daemonize` 三个调用点，在草稿纸上画出“哪一行执行在哪一代进程上”。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉第一次 `fork_and_exit`，直接调用 `setsid()`，会发生什么？

> **参考答案**：当 `wireguard-rs` 是从交互式 shell 启动时，它往往是其进程组的组长，`setsid()` 会返回 `-1` 失败，于是 `daemonize` 直接返回 `Err(SetSession)`，`main.rs` 退出码 `-5`。第一次 fork 的存在正是为了确保调用 `setsid` 的进程一定不是组长。

**练习 2**：第二次 fork（“再 fork 一次创造孤儿”）去掉行不行？为什么本项目仍然保留它？

> **参考答案**：从“脱离终端”的角度看，只 fork 一次 + `setsid` 已经够了。第二次 fork 是一个**加固**：让最终的 daemon 不再是会话首领，从而无法通过 `open` 一个 tty 重新获得控制终端。本项目保留它属于遵循经典 daemon 套路的防御性写法。

### 4.2 特权降级 drop_privileges

#### 4.2.1 概念说明

`wireguard-rs` 启动时必须以 root 身份完成两件特权操作（见 [u1-l1](u1-l1-overview.md) / [u1-l4](u1-l4-main-entry.md)）：

1. 在 `/var/run/wireguard/` 下绑定 UAPI 套接字；
2. 通过 `/dev/net/tun` 创建 TUN 虚拟网卡。

这两件事一旦完成，进程此后就**再也不需要 root 了**。继续以 root 跑一个长期在线的网络服务是危险的——一旦它被攻破（比如协议层出现一个内存安全漏洞），攻击者就拿走了整台机器的 root。因此 `drop_privileges` 的目标是：**用最小代价，把进程永久性地降级为 `nobody`，并尽量缩小它能看到的世界**。

它做了三件事：

- **`chroot("/tmp")`**：把进程的“根目录”改到 `/tmp`，让它再也看不到系统其它目录（一种轻量沙箱）。
- **`umask(0)`**：清空文件创建掩码，避免继承 shell 留下的可疑默认权限。
- **`setgid` + `setuid`**：把组号和用户号都切到 `nobody`，**永久放弃** root（uid 0）。

#### 4.2.2 核心流程

步骤顺序由第 2.3 节的两条约束严格决定：

```text
getpwnam("nobody")   ← 必须在 chroot 之前，因为它要读 /etc/passwd
   │ (取得 nobody 的 uid / gid)
   ▼
getuid()==0 ?        ← 只有 root 才能 chroot
   ├─ 是 → chroot("/tmp")     ← 必须在 setuid 之前（chroot 要 root）
   └─ 否 → 跳过
   ▼
umask(0)             ← 清权限掩码
   ▼
chdir("/")           ← 进入新的根（其实是 /tmp）
   ▼
setgid(nobody.gid)   ← 必须在 setuid 之前（setuid 后再也无权 setgid）
   ▼
setuid(nobody.uid)   ← 单向降级，到此永久脱离 root
```

其中 `umask(0)` 对将来进程创建文件的权限有一个干净的小公式。设程序请求的权限位是 `requested`、umask 是 `m`，则实际落到磁盘的权限为：

\[
\text{actual} = \text{requested}\ \&\ \sim m
\]

把 `m` 设为 0，意味着 `actual == requested`，权限完全由程序自己的请求决定，不被 shell 环境偷偷收紧或放宽。这是一个“消除环境不确定性”的小而重要的细节。

#### 4.2.3 源码精读

[drop_privileges：从 root 降级为 nobody 的有序步骤](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/util.rs#L53-L85)

```rust
pub fn drop_privileges() -> Result<(), DaemonizeError> {
    // 取得 nobody 的 uid & gid（必须在 chroot 之前！）
    let usr = unsafe { getpwnam("nobody\x00".as_ptr() as *const c_char) };
    if usr.is_null() {
        return Err(DaemonizeError::SetGroup);
    }

    let uid = unsafe { getuid() };
    if uid == 0 && unsafe { chroot("/tmp\x00".as_ptr() as *const c_char) } != 0 {
        return Err(DaemonizeError::Chroot);          // 仅 root 才 chroot
    }

    unsafe { umask(0) };                              // 清掩码

    if unsafe { chdir("/\x00".as_ptr() as *const c_char) } != 0 {
        return Err(DaemonizeError::Chdir);            // 进入新根
    }

    if unsafe { setgid((*usr).pw_gid) } != 0 {        // 先降组
        return Err(DaemonizeError::SetGroup);
    }

    // 再降用户（尾表达式，决定函数返回值）
    if unsafe { setuid((*usr).pw_uid) } != 0 {
        Err(DaemonizeError::SetUser)
    } else {
        Ok(())
    }
}
```

几个要点逐条对应：

- `getpwnam("nobody\x00")`：查询系统账号数据库得到 `nobody` 的 `passwd` 结构（含 `pw_uid`/`pw_gid`）。注意字符串末尾显式带 `\x00`，因为这里是把字节指针直接喂给 C 函数，C 字符串必须以 NUL 结尾。**这步必须在 `chroot` 之前**，否则改根后再也读不到 `/etc/passwd`。
- `uid == 0 && chroot(...)`：用短路求值把“是 root 才尝试 chroot”写得只有一行。`chroot` 返回 0 成功、非 0 失败。
- `setgid` 在 `setuid` **之前**：root 可以随意 `setgid`；一旦 `setuid` 到非 root，再想 `setgid` 就受限制了。所以必须趁还是 root 时先把组也降掉。
- 末尾的 `if setuid(...) { Err } else { Ok }` 是一个**尾表达式**，没有分号，其值就是整个函数的返回值。

> 术语提示：`getpwnam` 返回的是指向 libc 静态缓冲区的指针，可能为空（用户不存在）。代码用 `usr.is_null()` 判空，空指针解引用是 UB，所以这一步防御必不可少。

#### 4.2.4 代码实践

**实践目标**：为 `drop_privileges` 增加可观测性，记录“是否以 root 启动”以及“最终切换到的 uid/gid”，并验证 `--disable-drop-privileges` 开关的行为路径。

**操作步骤**：

1. 在 [src/util.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/util.rs) 的 `drop_privileges` 里，在 `getuid()` 之后加一行记录“当前是否 root”：

   ```rust
   let uid = unsafe { getuid() };
   log::info!("drop_privileges: started as root? {}", uid == 0);
   ```

   并在 `setuid` 成功的分支里加一行记录降级后的身份：

   ```rust
   if unsafe { setuid((*usr).pw_uid) } != 0 {
       Err(DaemonizeError::SetUser)
   } else {
       log::info!(
           "drop_privileges: now running as uid={} gid={}",
           (*usr).pw_uid, (*usr).pw_gid
       );
       Ok(())
   }
   ```

2. **关键陷阱（务必先想清楚再验证）**：`main.rs` 里日志初始化发生在 `drop_privileges` **之后**——见下文 4.3.3 引用的 [main.rs 日志初始化](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L120-L124)。因此如果你直接用 `log::info!`，这些日志**不会输出**，因为 `env_logger` 还没起来。有两个修正方案二选一：
   - 用 `eprintln!`（直接写 stderr，不依赖 logger）替换上面的 `log::info!`；或
   - 把 `main.rs` 里的 `env_logger::builder().try_init()` 上移到 `drop_privileges()` 调用之前，并设置环境变量 `RUST_LOG=info`。
3. 用 `cargo build --release` 编译，分别以两种方式启动（需要 root）：
   - `sudo ./target/release/wireguard-rs wg0 -f`（前台、会降权）
   - `sudo ./target/release/wireguard-rs wg0 -f --disable-drop-privileges`（前台、不降权）

**需要观察的现象**：

- 第一种方式下，应看到“started as root? true”以及“now running as uid=65534 gid=65534”（`nobody` 的具体号码因发行版而异，常见为 65534 或 99）。
- 第二种方式下（`--disable-drop-privileges`）：因为 [main.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L97-L106) 用 `if drop_privileges { ... }` 把整个调用包起来了，开关一关，`drop_privileges` 函数**根本不会被调用**，所以你**看不到任何一行** drop_privileges 的日志。这本身就是要验证的结论：开关的语义不是“调用但跳过降权”，而是“整个函数都不进入”。

**预期结果 / 待本地验证**：降权路径下日志显示 uid 从 0 变为 `nobody`；`--disable-drop-privileges` 路径下无 drop_privileges 日志、且进程此后仍为 root（可用 `ps -o uid= -p <pid>` 验证，仍为 0）。若本环境无 root 权限，请标注「待本地验证」，并改为源码阅读：沿 `main.rs` 第 57–74 行的参数解析，确认 `--disable-drop-privileges` 只是把布尔变量置假，并不改变后续 UAPI/TUN 的创建顺序。

#### 4.2.5 小练习与答案

**练习 1**：把 `setgid` 和 `setuid` 的顺序对调（先 `setuid` 再 `setgid`），会出什么问题？

> **参考答案**：先 `setuid((*usr).pw_uid)` 之后进程已不再是 root。此时再调用 `setgid` 会因为没有足够权限而失败（返回非 0），`drop_privileges` 返回 `Err(SetGroup)`，整个降权失败。即便侥幸成功，也只能切到自己所在组，达不到“同时切到 nobody 的组”的目的。所以必须先 `setgid` 后 `setuid`。

**练习 2**：为什么 `getpwnam` 不能放到 `chroot` 之后调用？

> **参考答案**：`chroot("/tmp")` 之后，进程眼中的 `/etc/passwd` 其实是 `/tmp/etc/passwd`，通常并不存在，`getpwnam("nobody")` 会失败返回空指针，于是函数提前返回 `Err(SetGroup)`。因此查账号信息必须在改根之前完成，代码也正是这么排的。

### 4.3 错误类型 DaemonizeError 与 main.rs 的调用时机

#### 4.3.1 概念说明

`util.rs` 里两个函数都可能失败，且失败原因各不相同（fork 失败、setsid 失败、chroot 失败、setuid 失败……）。项目用一个专门的枚举 `DaemonizeError` 把这些失败原因枚举清楚，并实现了 `Display`，让 `main.rs` 可以直接 `eprintln!("Failed to ...: {}", e)` 打印出人话。

#### 4.3.2 核心流程

调用链与失败处理：

```text
main()
  ├─ 解析 --disable-drop-privileges / -f
  ├─ plt::UAPI::bind()         ← 失败 exit(-2)
  ├─ plt::Tun::create()        ← 失败 exit(-3)
  ├─ if drop_privileges {
  │     util::drop_privileges()← 失败 exit(-4)
  │  }
  ├─ if !foreground {
  │     util::daemonize()      ← 失败 exit(-5)
  │  }
  └─ env_logger::builder()...  ← 日志在此之后才可用
```

每个失败点都配一个**不同的退出码**（-2、-3、-4、-5），方便运维从返回码一眼定位是哪一步挂了。

#### 4.3.3 源码精读

[DaemonizeError：六种失败原因的枚举](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/util.rs#L7-L15)

```rust
pub enum DaemonizeError {
    Fork,
    SetSession,
    SetGroup,
    SetUser,
    Chroot,
    Chdir,
}
```

六个变体分别对应六个可能失败的系统调用点。`Display` 实现把它们映射成英文人话：

[Display：把错误码翻译成可读消息](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/util.rs#L17-L29)

例如 `DaemonizeError::SetUser => "unable to set user (drop privileges)"`。这样 `main.rs` 就不必关心具体是哪个变体，直接 `{}` 格式化即可。

再看调用方。两个开关的解析在 main 的参数循环里：

[main 解析降权/前台两个开关](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L57-L74)

注意默认值：`drop_privileges` 默认 `true`（默认降权），`foreground` 默认 `false`（默认进后台）。只有显式传 `--disable-drop-privileges` 或 `-f` 才改变它们。

调用 `drop_privileges` 与 `daemonize` 的这段是本讲的关键：

[main 调用 drop_privileges 与 daemonize，每步独立失败处理](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L97-L117)

```rust
// drop privileges
if drop_privileges {
    match util::drop_privileges() {
        Ok(_) => (),
        Err(e) => { eprintln!("Failed to drop privileges: {}", e); exit(-4); }
    }
}

// daemonize to background
if !foreground {
    match util::daemonize() {
        Ok(_) => (),
        Err(e) => { eprintln!("Failed to daemonize: {}", e); exit(-5); }
    }
}
```

这里有三点值得注意：

1. **顺序：先降权，再 daemonize**。降权必须趁进程还是 root 时做（尤其 `chroot` 要 root）；而 `daemonize` 的 fork 本身不需要 root，放后面无妨。反过来若先 fork 再降权，父子两份都要各自降权，徒增复杂度。
2. **顺序：降权在 UAPI bind 与 TUN create 之后**。因为后两者需要 root，所以 root 必须“撑到”它们做完才能丢——这正是 [u1-l4](u1-l4-main-entry.md) 强调的“先绑 UAPI、建 TUN，再降权”的根因。
3. **日志在两者之后才初始化**：

[main 在降权/守护进程化之后才初始化日志](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L120-L124)

```rust
env_logger::builder()
    .try_init()
    .expect("Failed to initialize event logger");
log::info!("Starting {} WireGuard device.", name);
```

这条信息直接决定了 4.2.4 实践里“为什么 `log::info!` 在 `drop_privileges` 内不会输出”——日志系统此刻还没被建起来。要观察降权过程，要么用 `eprintln!`，要么把日志初始化前移。

#### 4.3.4 代码实践

**实践目标**：用退出码这个“外部可观测信号”验证失败处理路径，无需真正触发失败。

**操作步骤**（源码阅读型实践，不改源码）：

1. 读 [src/main.rs:97-117](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L97-L117)，列出每个 `exit(-N)` 分别对应哪一步失败，填入下表：

   | 退出码 | 触发位置 | 失败原因（来自哪个 `DaemonizeError` 变体） |
   | ------ | -------- | ------------------------------------------ |
   | -2     | UAPI bind | （非 DaemonizeError，平台错误） |
   | -3     | TUN create | （非 DaemonizeError，平台错误） |
   | -4     | drop_privileges | SetGroup / Chroot / Chdir / SetUser |
   | -5     | daemonize | Fork / SetSession |

2. 思考题：如果你想让“`chroot` 失败”和“`setuid` 失败”显示**不同**的退出码（而不是都归到 -4），需要在 `main.rs` 做什么改动？

**需要观察的现象 / 预期结果**：你应当发现，当前 `main.rs` 对 `drop_privileges` 内部的**所有**失败变体都一视同仁地返回 `-4`，对 `daemonize` 的所有变体都返回 `-5`。要区分到变体级别，需要把 `match e { DaemonizeError::Chroot => exit(-40), DaemonizeError::SetUser => exit(-41), … }` 展开。

**待本地验证**：如有 root 环境且 `nobody` 用户不存在（某些极简容器），可直接触发 `SetGroup` 路径，观察退出码是否为 -4。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DaemonizeError` 只实现了 `Display`，却没有实现 `std::error::Error`？

> **参考答案**：这是项目的取舍。实现 `std::error::Error` 通常是为了把错误向上传播、与 `?` 操作符及 `Box<dyn Error>` 体系配合。但本项目的 `util` 函数只在 `main.rs` 里被调用一次、立即 `eprintln!` + `exit`，并不需要向上层 API 传播，所以只实现 `Display`（用于打印）就够用了，没必要引入完整的 error-trait 体系。

**练习 2**：把 `main.rs` 里的 `drop_privileges()` 和 `daemonize()` 调用顺序对调（先 daemonize 再 drop_privileges）会发生什么？

> **参考答案**：`daemonize` 会 fork 出子进程；随后子进程才执行 `drop_privileges`——这本身能跑通，但有两个问题：(1) 原始父进程在 daemonize 的第一次 fork 后就已 `exit`，无法再做降权；(2) 更微妙的是，若把降权放在 daemonize **之后**，则 fork 出来的孙进程才降权，而它此时已经脱离了原终端，调试更困难。本项目选择“先降权、后 daemonize”是更清晰的顺序：趁还在前台、还是 root，先把权限降掉，再去后台化。

## 5. 综合实践

把本讲的三块知识串起来，完成一个“启动安全检查清单”：

**任务**：基于 [src/util.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/util.rs) 与 [src/main.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs)，写一份 **启动安全审计笔记**（Markdown 即可，不写入仓库），要求覆盖：

1. **时序图**：画出从 `main()` 开始，经 UAPI bind → TUN create → drop_privileges → daemonize → 日志初始化 → 工作线程的完整时间线，并标注每一步的进程身份（root / nobody）、进程代际（原始进程 / 孙进程）。
2. **顺序约束表**：列出 `drop_privileges` 内部 6 个系统调用之间所有的“必须先后”关系，并各给一句话理由。例如：`getpwnam` 必须早于 `chroot`，理由是 chroot 后读不到 `/etc/passwd`。
3. **风险点**：指出至少一个“如果顺序错了会出安全问题”的点。例如：如果 `setuid` 在 `chroot` 之前，则进程已经降权、`chroot` 会失败、降权半途而废（进程仍可能持有部分 root 能力）。
4. **可观测性改进**：基于 4.2.4 的发现，提出一种“既能看到降权过程、又不破坏现有日志初始化顺序”的最小改动方案（提示：`eprintln!` 或前移 `env_logger` 初始化）。

完成后，你应该能用一句话向别人解释清楚：**为什么 `wireguard-rs` 启动时“先以 root 绑套接字建网卡、立刻降权到 nobody、再后台化”这个顺序是安全的，而任何调换都会破坏安全性。**

## 6. 本讲小结

- `daemonize()` 采用经典“双 fork + `setsid`”：第一次 fork 让 `setsid` 的调用者不是组长，`setsid` 脱离控制终端，第二次 fork 保证 daemon 永远无法重新获得控制终端。
- `fork_and_exit()` 把“fork、父死子活”这一模式抽出来复用，依据 `fork()` 返回值的三段（负/零/正）区分失败、子进程、父进程。
- `drop_privileges()` 的步骤顺序由两条硬约束决定：需要 root 的操作（`chroot`、`setgid`）必须在 `setuid` 之前；需要读 `/etc/passwd` 的 `getpwnam` 必须在 `chroot` 之前。
- `setgid` 必须先于 `setuid`，因为 `setuid` 之后进程就失去了自由 `setgid` 的能力——这是降权“单向性”的直接体现。
- `DaemonizeError` 用 6 个变体枚举所有失败点，`Display` 把它们翻译成人话；`main.rs` 给降权和守护进程化各分配了独立退出码（-4 / -5）。
- `main.rs` 的调用顺序是“绑 UAPI → 建 TUN → 降权 → daemonize → 初始化日志”：前两步需要 root，所以 root 要撑到这时才能丢；而日志在降权之后才初始化，意味着 `drop_privileges` 内的 `log::` 调用默认不会输出。

## 7. 下一步学习建议

本讲结束后，入门单元（u1）的“运行与启动”部分就完整了。建议：

- **紧接着学 [u2-l1 平台抽象 trait 设计](u2-l1-platform-traits.md)**：理解 `plt::UAPI`/`plt::Tun`/`plt::UDP` 这三个别名背后是怎样一组 trait，它们正是本讲里“需要 root 才能绑/创建”的两个对象的抽象定义。
- **回顾性阅读**：回头重读 [main.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs) 全文，此时你应该能把从参数解析到 `wg.wait()` 的每一步都对应到本系列讲义里的某一讲。
- **延伸阅读（可选）**：Linux 的 `capabilities(7)`、`prctl(PR_SET_NO_NEW_PRIVS)` 与 `seccomp`，它们是比 `setuid`/`chroot` 更细粒度的现代降权手段；本项目走的是经典 Unix 路线，了解现代手段有助于对比其取舍。
