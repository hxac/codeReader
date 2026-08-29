# build_to：批量构建与输出目录管理

## 1. 本讲目标

上一讲（u2-l2）我们精读了 `main` 的命令分发、`print_usage` 的输出分流和 `check_mdbook` 的子进程探测，但把 `build_to` 当作黑盒留了下来。本讲就打开这个黑盒。学完本讲，你应该能：

1. 解释 `project_root` 为什么用编译期宏 `env!("CARGO_MANIFEST_DIR")` 定位项目根，而不是依赖运行时的工作目录。
2. 逐步梳理 `build_to` 的「清理 → 重建 → 逐书构建 → 计数 → 落地页 → `.nojekyll` → 收尾」全流程。
3. 说出 `.nojekyll` 这个空文件为什么必须存在。
4. 区分 `site/` 与 `docs/` 两个输出目录分别被哪条命令生产、被哪个下游环节消费，以及 `build` 与 `deploy` 两个子命令的全部差异。

## 2. 前置知识

本讲会用到几个基础概念，先用通俗语言解释：

- **编译期宏与运行时取值**：Rust 的 `env!("VAR")` 是宏，在**编译那一刻**读取环境变量 `VAR` 的值，并把值作为字符串字面量直接烧进二进制；而 `std::env::var("VAR")` 是普通函数，在**程序运行时**读取。前者拿到的是「编译发生地的快照」，后者拿到的是「运行时的现场」。这个区别正是 `project_root` 的核心。
- **当前工作目录（cwd）**：进程启动时继承的目录。用 `cargo xtask build` 时，你的 shell 可能在仓库任何子目录里，所以**不能**假设 cwd 就是仓库根。
- **干净构建（clean build）**：先把输出目录整个删掉再重建，保证产物目录里没有上一次构建留下的过期文件。与之相对的是增量式「往旧目录里覆盖」，后者会累积孤儿文件（比如某本书改名后，旧目录还躺在那里）。
- **子进程退出状态**：父进程（xtask）调用 `Command::status()` 启动子进程（mdbook）并等待，拿到一个退出状态对象，`.success()` 判断是否成功。上一讲 `check_mdbook` 已用过同一套 API。
- **Jekyll 与 GitHub Pages**：GitHub Pages 默认用 Jekyll（一个静态博客生成器）处理你发布的文件——它会套用模板语法、忽略以下划线开头的文件等。对于「已经是最终 HTML」的站点（mdBook 的输出），这些处理不但无用，还可能吞掉文件。`.nojekyll` 是 GitHub 官方约定的开关文件：站点根目录下存在它，Pages 就完全跳过 Jekyll。
- **前置讲义衔接**：u2-l1 讲过 workspace 与 cargo 别名；u2-l2 讲过 `cmd_build`/`cmd_deploy` 进入 `build_to` 之前会先 `check_mdbook`，探测失败立即以退出码 1 终止——所以本讲讨论 `build_to` 时可以假定 `mdbook` 一定存在。u1-l2 讲过 `BOOKS` 常量是（slug, title, description, category）四元组注册表，本讲只用到它的 slug 字段和遍历能力。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [xtask/src/main.rs](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs) | 本讲主角：`project_root`、`build_to`、`cmd_build`、`cmd_deploy`、`cmd_clean`，以及 `site/` 的消费者 `cmd_serve` |
| [README.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md) | 维护者一节写明四个子命令的输出目录约定与部署方式 |
| [.github/workflows/pages.yml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml) | `docs/` 的消费者：CI 里 `cargo xtask deploy` 之后把 `./docs` 上传为 Pages artifact |
| [.gitignore](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.gitignore) | 证明 `site/`、`docs/` 与 `**/book/` 都是**不入库**的构建产物 |
| [async-book/book.toml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml) | 对照物：单本书自己的 `build-dir = "book"` 会被 xtask 的 `--dest-dir` 覆盖 |

## 4. 核心概念与源码讲解

### 4.1 project_root：编译期定位项目根

#### 4.1.1 概念说明

`build_to` 要做的一切都围绕路径展开：书籍源码在 `<仓库根>/<slug>/`，产物要写进 `<仓库根>/site/` 或 `<仓库根>/docs/`。第一个问题就是——xtask 运行时怎么知道仓库根在哪？

直观的方案有两种：

1. **用运行时 cwd**：`std::env::current_dir()`。问题是用户可以在任何目录调用 `cargo xtask`（比如在 `async-book/` 里），cwd 就不是仓库根了。
2. **用 xtask 自己的物理位置**：Cargo 编译任何 crate 时都会设置 `CARGO_MANIFEST_DIR` 环境变量，值为**该 crate 的 `Cargo.toml` 所在目录**。用 `env!` 宏在编译期取出，等于把「xtask 必然位于 `<仓库根>/xtask/`」这一事实固化进二进制——再取 `.parent()` 就得到仓库根。

本仓库选了方案 2。它的隐含契约写在 `expect` 的错误消息里：xtask 必须住在 workspace 的子目录中，`.parent()` 才有意义。

#### 4.1.2 核心流程

```text
编译期：Cargo 设置 CARGO_MANIFEST_DIR = <仓库根>/xtask
         env!("CARGO_MANIFEST_DIR") 把该路径烧进二进制（&'static str）

运行时：project_root()
           = Path::new("<仓库根>/xtask").parent()   // 去掉最后一段
           = PathBuf("<仓库根>")
```

两个值得注意的性质：

- **与 cwd 完全无关**。无论从哪里启动，`project_root()` 返回同一个绝对路径。
- **与「二进制被拷到哪里」也无关，但与「源码目录结构」强相关**。如果你把编译好的 xtask 二进制单独复制到别的机器上运行，它仍然指向编译时的那个路径——这就是编译期定位的代价。对本仓库（总是通过 `cargo run --package xtask` 在原地编译运行）来说毫无问题。

#### 4.1.3 源码精读

[xtask/src/main.rs:54-59](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L54-L59) —— 用编译期环境变量定位仓库根：

```rust
fn project_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("xtask must live in a workspace subdirectory")
        .to_path_buf()
}
```

这段代码做了三件事：`env!` 在编译期内嵌 xtask 的清单目录（即 `<仓库根>/xtask`）；`.parent()` 去掉 `xtask` 这一段得到仓库根；`.expect(...)` 在假设被破坏（路径取不到父级）时以 panic 终止，错误消息本身就是设计约束的文档。

#### 4.1.4 代码实践

1. **实践目标**：亲眼验证 `project_root` 与运行时 cwd 无关。
2. **操作步骤**：
   ```bash
   cd async-book        # 故意进入子目录
   cargo xtask build    # cargo 会向上找到仓库根的 workspace 与 .cargo/config.toml
   ls ../site           # 产物在仓库根下
   ls site              # 本地目录下没有 site（预期报 No such file）
   ```
3. **需要观察的现象**：构建日志照常输出，`site/` 出现在**仓库根**而不是 `async-book/` 里。
4. **预期结果**：因为路径来自编译期宏而非 cwd，产物位置与调用位置无关。（cargo 从子目录发现上级 workspace 与别名配置属于其标准行为，但整条链路在 `async-book` 下能否走通请以实际输出为准——待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：把 `env!("CARGO_MANIFEST_DIR")` 换成 `std::env::var("CARGO_MANIFEST_DIR").unwrap()` 会发生什么？

**答案**：语义完全改变。`std::env::var` 在**运行时**读取环境变量，而 `cargo run` 运行二进制时通常不会设置 `CARGO_MANIFEST_DIR`（它是给**构建脚本和编译过程**用的），所以大概率 panic 或得到错误路径；即便设置了，也反映的是运行现场而非编译现场。这正是「想用编译期常量就必须用 `env!` 宏」的原因。

**练习 2**：如果维护者把 `xtask` 目录改名为 `tools/`（并相应更新 workspace members），`project_root` 还正确吗？

**答案**：仍然正确。`project_root` 只依赖「xtask crate 位于仓库根的**直接**子目录」这一事实，不关心目录叫什么名字。但如果把 xtask 嵌套两层（如 `dev/xtask/`），`.parent()` 只能去掉一段，返回的会是 `dev/` 而不是仓库根——`expect` 的消息无法覆盖这种情况，会静默地算错。

### 4.2 build_to：清理-构建-计数-收尾

#### 4.2.1 概念说明

`build_to` 是 `build` 和 `deploy` 共享的构建引擎，唯一的参数是输出目录名。它解决的问题：把 `BOOKS` 注册表里的七本书，从各自独立的 mdBook 源码目录，汇编成一个**统一站点**——每本书是站点下的一个子目录，外加一张落地页索引。

它把四件独立的事按固定顺序串起来：

1. **输出区管理**——先删后建，保证干净；
2. **批量构建**——遍历 `BOOKS`，逐书起 mdbook 子进程；
3. **收尾装配**——生成落地页 `index.html`、写入 `.nojekyll`；
4. **进度反馈**——✓/✗ 逐书打印、末尾给出成功计数。

#### 4.2.2 核心流程

```text
build_to(dir_name):
    root ← project_root()
    out  ← root / dir_name                 # 例如 <repo>/site

    if out 存在: remove_dir_all(out)        # ① 清理：整个删掉
    create_dir_all(out)                     # ② 重建：空的输出区

    ok ← 0
    for (slug, ...) in BOOKS:               # ③ 遍历注册表（不是扫描磁盘）
        if root/slug 不是目录:
            打印 "✗ slug/ not found, skipping"
            continue                        #    缺目录：告警并跳过
        dest ← out / slug
        status ← 运行子进程:
                     mdbook build --dest-dir dest
                     （cwd = root/slug）
        if status.success(): 打印 "✓ slug"; ok ← ok + 1
        else:               打印 "✗ slug FAILED"   # 失败不中断循环

    打印 "ok / BOOKS.len() books built"     # ④ 计数
    write_landing_page(out)                  # ⑤ 落地页（下一讲精读）
    写空文件 out/.nojekyll                   # ⑥ 关闭 Jekyll
```

几个设计要点：

- **遍历注册表而非磁盘**：循环源是 `BOOKS` 常量。磁盘上存在但未注册的目录会被完全忽略；已注册但目录缺失的只是跳过并告警（u1-l2 已建立这一认知，这里看到了它的实现）。
- **`--dest-dir` 覆盖单书配置**：每本书的 `book.toml` 写着 `build-dir = "book"`（见 [async-book/book.toml:7-8](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L7-L8)），单独构建时输出到 `async-book/book/`（`.gitignore` 里的 `**/book/` 排除的就是它）。批量构建时 xtask 用命令行参数 `--dest-dir <out>/<slug>` 把输出重定向进统一站点——命令行参数优先级高于配置文件，七本书各自的 `book-dir` 设置在批量模式下全部失效。
- **`current_dir` 设定子进程视角**：mdbook 以书籍目录为 cwd 启动，它读到的 `book.toml`、`src/` 都相对该目录；而输出路径 `dest` 是绝对路径，两者互不干扰。
- **失败容忍，不提前退出**：某一本书构建失败只打印 `✗ ... FAILED`，循环继续处理其余书。注意 `build_to` **没有任何 `std::process::exit` 调用**——即使 7 本里挂了 6 本，xtask 进程的退出码仍然是 0。
- **`ok` 与 `BOOKS.len()`**：末尾的 `6/7 books built` 一眼可见缺失，这是给人看的信号，不是给 shell 看的退出码。

#### 4.2.3 源码精读

[xtask/src/main.rs:128-137](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L128-L137) —— 准备阶段：定位根、拼接输出路径、先删后建、打印横幅：

```rust
fn build_to(dir_name: &str) {
    let root = project_root();
    let out = root.join(dir_name);

    if out.exists() {
        fs::remove_dir_all(&out).expect("failed to clean output dir");
    }
    fs::create_dir_all(&out).expect("failed to create output dir");

    println!("Building unified site into {dir_name}/\n");
```

`exists()` 判断让首次构建（目录尚不存在）也能安全通过；随后的 `create_dir_all` 保证空目录就绪。

[xtask/src/main.rs:139-152](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L139-L152) —— 构建循环的入口与 mdbook 子进程调用：

```rust
    let mut ok = 0u32;
    for &(slug, _, _, _) in BOOKS {
        let book_dir = root.join(slug);
        if !book_dir.is_dir() {
            eprintln!("  ✗ {slug}/ not found, skipping");
            continue;
        }
        let dest = out.join(slug);
        let status = Command::new("mdbook")
            .args(["build", "--dest-dir"])
            .arg(&dest)
            .current_dir(&book_dir)
            .status()
            .expect("failed to run mdbook — is it installed?");
```

注意解构模式 `&(slug, _, _, _)`：只用 slug，其余三个字段（标题、描述、分类）在本函数中以下划线丢弃——它们是落地页生成才消费的数据。错误分流也遵循 u2-l2 讲过的约定：跳过与失败走 `eprintln!`（stderr），成功走 `println!`（stdout）。

[xtask/src/main.rs:154-161](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L154-L161) —— 状态判断、计数与总结：

```rust
        if status.success() {
            println!("  ✓ {slug}");
            ok += 1;
        } else {
            eprintln!("  ✗ {slug} FAILED");
        }
    }
    println!("\n  {ok}/{} books built", BOOKS.len());
```

`ok` 显式声明为 `u32`——计数器语义；`BOOKS.len()` 是注册表长度，即使有目录被跳过，分母也固定为 7，缺了哪本从计数与上面的 ✗ 行对照可查。

[xtask/src/main.rs:163-167](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L163-L167) —— 收尾：落地页、`.nojekyll`、完成提示：

```rust
    write_landing_page(&out);

    // Prevent GitHub Pages from processing the output with Jekyll
    fs::write(out.join(".nojekyll"), "").expect("failed to create .nojekyll");
    println!("\nDone! Output in {dir_name}/");
```

`write_landing_page` 是 4.3、4.4 之外的下一讲（u2-l4）主角，本讲只把它当作「往 `out/index.html` 写一张卡片式索引页」的黑盒。

#### 4.2.4 代码实践

1. **实践目标**：脱离 xtask，手动复现它为**单本书**执行的那条命令，理解 `--dest-dir` 的作用。
2. **操作步骤**：
   ```bash
   cd async-book
   mdbook build --dest-dir /tmp/one-book     # 与 xtask 循环体内的调用同构
   ls /tmp/one-book                          # 应看到 index.html、章节数字目录等
   mdbook build                              # 对照：不带 --dest-dir
   ls book                                   # 输出落进了 book.toml 的 build-dir
   ```
3. **需要观察的现象**：第一次构建的产物出现在 `/tmp/one-book`（命令行参数生效）；第二次出现在 `async-book/book/`（回退到配置文件）。
4. **预期结果**：两次产物内容一致，只有位置不同——证明批量构建的「统一站点结构」完全由 `--dest-dir out/<slug>` 这一个参数实现，书籍源码无需任何改动。（本机 mdbook 版本若与维护者钉住的 0.4.52 不同，个别产物文件可能有差异——待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：假设维护者把 `BOOKS` 里的 `async-book` 改名为 `async-rust-book`（磁盘目录同步改名），旧产物目录 `site/async-book/` 会怎样？

**答案**：消失。`build_to` 开头 `remove_dir_all` 把整个 `site/` 删掉重建，`site/async-book/` 不可能幸存。这正是「先删后建」相对「增量覆盖」的核心收益：绝不会留下与注册表不再对应的孤儿目录。

**练习 2**：如果 `python-book` 的某章里有非法 Markdown 导致 mdbook 构建失败，`cargo xtask build` 的进程退出码是多少？其余六本书会构建吗？

**答案**：退出码是 0，其余六本照常构建。失败分支只 `eprintln!("  ✗ {slug} FAILED")`，不 `exit`、不 `return`，循环继续；`build_to` 及其调用链上都没有以非零码退出的路径（唯一的非零退出在更早的 `check_mdbook` 阶段）。日志会显示 `6/7 books built`，人能看出问题，但 CI 判定「通过」——这是当前实现的已知特性，也是想要「构建失败就阻断流水线」时需要改造的点。

**练习 3**：为什么循环里用 `book_dir.is_dir()` 而不是 `book_dir.exists()`？

**答案**：`is_dir()` 同时排除「不存在」和「存在但不是目录」两种情况（比如仓库根下恰好有个名为 `c-cpp-book` 的**文件**）。用 `exists()` 的话，后一种情况会漏进构建分支，让 mdbook 在错误的 cwd 下运行，报出更难定位的错误。提前用类型判断过滤，失败消息 `✗ {slug}/ not found, skipping` 更贴近真实原因。

### 4.3 .nojekyll：一个空文件的任务

#### 4.3.1 概念说明

构建完成后，`build_to` 做的最后一件实事是往输出根写一个**零字节**的 `.nojekyll` 文件。它没有任何内容——GitHub Pages 只检查「这个文件是否存在」，存在即表示：**请把这个目录当作纯静态文件原样发布，跳过 Jekyll 处理**。

为什么 mdBook 站点需要它？GitHub Pages 默认对发布目录跑 Jekyll，而 Jekyll 有自己的文件筛选规则——最典型的是以下划线 `_` 开头的文件/目录被视为 Jekyll 内部结构而不被发布。静态站点生成器的产物里若恰好有这类文件名（或将来出现），就会被无声吞掉。`.nojekyll` 一劳永逸地关掉这层「好心的」处理。

注意时机：这个文件写在 `build_to` 的收尾阶段，**每次构建都会重新生成**——因为输出目录刚被整体删除重建，上一轮写入的 `.nojekyll` 已经没了，不重写就会丢失。这解释了为什么它必须内建在构建流程里，而不能靠「手动放一次」。

#### 4.3.2 核心流程

```text
mdbook 输出（HTML/CSS/JS/字体）
        │
        ▼
out/index.html  ←— write_landing_page 生成落地页
out/.nojekyll   ←— fs::write 写入空字符串
        │
        ▼ 上传到 GitHub Pages 后
存在 .nojekyll ⇒ Pages 跳过 Jekyll ⇒ 文件原样发布
```

#### 4.3.3 源码精读

[xtask/src/main.rs:165-166](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L165-L166) —— 在输出根写入空标记文件：

```rust
    // Prevent GitHub Pages from processing the output with Jekyll
    fs::write(out.join(".nojekyll"), "").expect("failed to create .nojekyll");
```

第二个参数是空字符串 `""`，所以文件内容为 0 字节；注释一句话点明动机。配合上一节的 `remove_dir_all` 理解：删了重建的目录里，这一行是 `.nojekyll` 唯一的来源。

顺带对照清理入口 [xtask/src/main.rs:487-496](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L487-L496) —— `cmd_clean` 把 `site` 与 `docs` 一起删掉，与 `build_to` 的先删后建呼应：

```rust
fn cmd_clean() {
    let root = project_root();
    for dir_name in ["site", "docs"] {
        let dir = root.join(dir_name);
        if dir.exists() {
            fs::remove_dir_all(&dir).expect("failed to remove dir");
            println!("Removed {dir_name}/");
        }
    }
}
```

#### 4.3.4 代码实践

1. **实践目标**：确认 `.nojekyll` 确实存在、确实为空、确实每次重建。
2. **操作步骤**：
   ```bash
   cargo xtask build
   ls -la site/ | head       # -a 才能看到点开头的隐藏文件
   stat -c '%s %n' site/.nojekyll   # 打印文件大小与名字
   touch site/garbage.txt    # 手动污染输出目录
   cargo xtask build         # 重新构建
   ls site/garbage.txt       # 已不存在
   stat -c '%s %n' site/.nojekyll  # .nojekyll 却又回来了
   ```
3. **需要观察的现象**：`.nojekyll` 出现在 `site/` 根部、大小为 0；手动塞入的 `garbage.txt` 在第二次构建后消失，而 `.nojekyll` 依然存在。
4. **预期结果**：垃圾文件被「先删后建」清除，`.nojekyll` 由代码每次重写，两者形成对照——一个证明清理生效，一个证明收尾必跑。

#### 4.3.5 小练习与答案

**练习 1**：既然 mdBook 生成的站点当前并没有下划线开头的文件，能不能省掉 `.nojekyll`？

**答案**：当前能跑，但属于「依赖巧合」。主题资源、搜索索引的文件名不由本仓库控制（mdbook 升级、主题变更都可能引入新文件名），一旦出现 `_` 前缀文件就会被 Jekyll 静默吞掉且极难排查。防御性写一个空文件的成本接近零，属于典型的「用一行代码买保险」。

**练习 2**：如果把 `fs::write(out.join(".nojekyll"), "")` 移到 `remove_dir_all` 之前执行，会怎样？

**答案**：文件会被随后的 `remove_dir_all(&out)` 连同整个目录一起删掉，最终发布目录里没有 `.nojekyll`，Pages 会恢复 Jekyll 处理。顺序在「清理之后、收尾之中」不是随意的——这段代码每一行的位置都有约束。

### 4.4 build 与 deploy 的差异：同一条流水线，两个出口

#### 4.4.1 概念说明

`cmd_build` 与 `cmd_deploy` 的结构几乎完全对称：都是「先 `check_mdbook`，再 `build_to(目录名)`」。差异可以列成一张表：

| 维度 | `cargo xtask build` | `cargo xtask deploy` |
|------|--------------------|---------------------|
| 入口函数 | `cmd_build` | `cmd_deploy` |
| 输出目录 | `site/` | `docs/` |
| 核心引擎 | `build_to("site")` | `build_to("docs")`（同一函数） |
| mdbook 检查 | 有，错误信息带安装链接 | 有，错误信息更短 |
| 额外动作 | 无 | 打印一行发布提示 |
| 谁消费产物 | `cmd_serve`（本地 3000 端口静态服务器） | CI 上传 `./docs` 为 Pages artifact |
| 是否入库 | 否（`.gitignore` 排除） | 否（`.gitignore` 排除） |

也就是说：**两个子命令共享全部构建逻辑，只在「产物给谁」上分岔**。`site/` 是给本地开发者的（serve 会读取它），`docs/` 是给 GitHub Pages 的（CI 上传它）。目录名的差异不是口味问题，而是两个下游约定俗成的接口。

还有一个值得细看的点：`cmd_deploy` 打印的提示语说的是「commit docs/ 并启用 Pages 的 Deploy from a branch 模式」，而当前仓库的实际流水线（pages.yml）用的是**artifact 模式**——`actions/upload-pages-artifact` 上传 `./docs`，再由独立的 deploy job 用 `actions/deploy-pages` 发布，全程不提交 `docs/` 到分支（`.gitignore` 也明确排除了它）。提示语描述的是 GitHub 经典的手动部署路线，是留给不用这套 CI 的使用者的备选路径；两条路线的公共前提都是「`docs/` 是站点根」。

#### 4.4.2 核心流程

```text
cargo xtask build                          cargo xtask deploy
        │                                          │
   check_mdbook ──失败→ exit(1)               check_mdbook ──失败→ exit(1)
        │成功                                    │成功
        ▼                                        ▼
  build_to("site")                        build_to("docs")
        │                                        │
        ▼                                        ▼
  site/<七本书>/  site/index.html           docs/<七本书>/  docs/index.html
  site/.nojekyll                           docs/.nojekyll
        │                                        │
        ▼                                        ▼
  cmd_serve 读取 site/                     pages.yml 上传 ./docs 为 artifact
  （cargo xtask serve 自动衔接）            deploy job 用 deploy-pages 发布
```

#### 4.4.3 源码精读

[xtask/src/main.rs:101-116](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L101-L116) —— 一对对称的入口：差异只有目标目录名与一行提示：

```rust
fn cmd_build() {
    if !check_mdbook() {
        eprintln!("Error: 'mdbook' not found in PATH. Please install it: https://rust-lang.github.io/mdbook/guide/installation.html");
        std::process::exit(1);
    }
    build_to("site");
}

fn cmd_deploy() {
    if !check_mdbook() {
        eprintln!("Error: 'mdbook' not found in PATH.");
        std::process::exit(1);
    }
    build_to("docs");
    println!("\nTo publish, commit docs/ and enable GitHub Pages → \"Deploy from a branch\" → /docs.");
}
```

`site/` 的消费者在 [xtask/src/main.rs:406-412](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L406-L412) —— `cmd_serve` 硬编码读取 `site/`（这也是 `cargo xtask serve` 必须先 build 的原因，错误消息里写明了补救办法）：

```rust
fn cmd_serve() {
    let site = project_root().join("site");
    let site_canon = fs::canonicalize(&site).expect(
        "site/ not found — run `cargo xtask build` first (e.g. `cargo xtask serve` runs build automatically)",
    );
```

`docs/` 的消费者在 CI。首先看产物如何被上传——[.github/workflows/pages.yml:48-54](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L48-L54)：构建步骤跑的正是我们本讲的 `deploy` 子命令，随后把 `./docs` 打包成 Pages artifact：

```yaml
      - name: Build documentation
        run: cargo xtask deploy

      - name: Upload artifact
        uses: actions/upload-pages-artifact@v4
        with:
          path: ./docs
```

然后是独立的 deploy job——[.github/workflows/pages.yml:56-65](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L56-L65)：它 `needs: build`、不重新构建，只把上一 job 上传的 artifact 发布成站点：

```yaml
  deploy:
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    runs-on: ubuntu-latest
    needs: build
    steps:
      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@v5
```

最后用两份文档交叉验证「目录分工」的约定。[README.md:91-98](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L91-L98) 在维护者一节写明四个命令的输出目录，[.gitignore:1-7](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.gitignore#L1-L7) 则从版本控制侧确认 `site/`、`docs/` 与 `**/book/` 都是产物、不入库：

```gitignore
# mdbook build output
**/book/

# xtask output
site/
docs/
target/
```

#### 4.4.4 代码实践

1. **实践目标**：验证 `site/` 与 `docs/` 内容同构，并在 CI 配置里找到 `docs/` 的确切消费点。
2. **操作步骤**：
   ```bash
   cargo xtask clean                  # 清空两边
   cargo xtask build && cargo xtask deploy
   ls site/ && echo '---' && ls docs/          # 顶层结构应一致
   diff -rq site docs                          # 递归比较，报告差异文件
   grep -n 'docs' .github/workflows/pages.yml  # 找出 CI 里引用 docs 的行
   ```
3. **需要观察的现象**：`ls` 两边都列出七个书籍目录加 `index.html`（`ls` 不显示 `.nojekyll`，需 `ls -a`）；`diff -rq` 或许报少量差异（如搜索索引里的时间戳、绝对路径），但整体结构一致；`grep` 命中的关键行是 `run: cargo xtask deploy` 与 `path: ./docs`。
4. **预期结果**：同一 `build_to` 引擎产出的两个目录除时间性差异外内容相同；CI 中 `docs` 的消费点只有 upload 步骤的 `path: ./docs`，deploy job 不再触碰文件系统。（`diff -rq` 具体报哪些文件取决于 mdbook 是否在产物里嵌入时间类信息——待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：CI 的 deploy job 为什么不需要安装 Rust 和 mdbook？

**答案**：因为它用的 `actions/deploy-pages@v5` 只把 build job 上传的 Pages artifact 发布到 Pages 环境，不接触源码也不跑任何构建命令——产物已经在 artifact 里了。构建所需的全部工具链都封闭在 build job 中（pages.yml 的 build job 里安装 Rust、mdbook、mdbook-mermaid 并执行 `cargo xtask deploy`）。这就是「构建与发布分离」在权限上的好处：deploy job 只需要 `pages: write` 等发布权限，不需要拉起完整工具链。

**练习 2**：如果想让 `cargo xtask serve` 直接预览 `docs/`（而不是 `site/`），最小改动是什么？

**答案**：把 `cmd_serve` 里的 `project_root().join("site")` 改成 `join("docs")` 即可——`site/` 与 `docs/` 本就同构。但更合理的做法是不改：`serve` 面向本地迭代（build 语义），`docs/` 语义上属于 Pages 产物，保持两个出口、一个引擎的现状让目录名携带「给谁用」的信息。

**练习 3**：`cmd_deploy` 末尾的提示说「commit docs/」，但 `.gitignore` 排除了 `docs/`。这两者矛盾吗？

**答案**：不矛盾，但揭示了提示语的适用场景。「commit docs/ + Deploy from a branch」是 GitHub Pages 的经典手动路线，适用于**没有**这套 CI（或想在别的仓库复用）的人——那时需要去掉 ignore 规则把产物提交进分支。本仓库自己的流水线走 artifact 路线，从不提交 `docs/`。提示语是给旁路使用者的备注，不是本仓库 CI 的操作步骤；读懂这个差别，正是「看代码 + 看 CI + 看 gitignore 三方对照」的价值。

## 5. 综合实践

**任务：画一张「从源码到上线」的产物流向图，并用实验证明图中每条边。**

1. 运行 `cargo xtask clean`，确认 `site/`、`docs/` 都消失（`ls` 验证）。
2. 运行 `cargo xtask build`，记录日志中 ✓ 的个数与末尾计数；用 `ls -a site/` 列出顶层内容，核对：七个书籍目录（与 `BOOKS` 逐一对照）、`index.html`、`.nojekyll`。
3. 做干净构建实验：`touch site/stale.html` 后重新 `cargo xtask build`，验证 `stale.html` 消失而 `.nojekyll` 仍在。
4. 手动复现单书构建：`cd async-book && mdbook build --dest-dir /tmp/one-book`，用 `ls /tmp/one-book` 与 `site/async-book/` 对比，确认两者结构相同——这就是 `--dest-dir` 的全部作用。
5. 运行 `cargo xtask deploy`，用 `diff -rq site docs` 确认两个出口同构；再打开 `.github/workflows/pages.yml`，用 `grep -n` 标出「哪一步产生 docs/（`run: cargo xtask deploy`）」「哪一步消费 docs/（`path: ./docs`）」。
6. 产出一张图（mermaid 或手绘均可），从「七个书籍源码目录 + BOOKS 注册表」出发，经 `build_to`，分岔到 `site/ → cmd_serve → localhost:3000` 与 `docs/ → upload-pages-artifact → deploy-pages → microsoft.github.io/RustTraining` 两条终点，并在每条边上注明支撑它的源码行号或 CI 行号。

完成标准：图中任何一条边你都能回答「哪一行代码/配置实现了它」。

## 6. 本讲小结

- `project_root` 用编译期宏 `env!("CARGO_MANIFEST_DIR")` 把「xtask 位于仓库根直接子目录」固化为路径常量，构建行为与运行时 cwd 完全解耦。
- `build_to` 是 build/deploy 共享的引擎：先删后建保证干净构建，遍历 `BOOKS` 注册表（而非扫描磁盘），用 `mdbook build --dest-dir <out>/<slug>` 把各书输出重定向进统一站点，单本失败不中断循环但也不改变退出码。
- `--dest-dir` 的命令行参数优先于各书 `book.toml` 的 `build-dir = "book"`，这让批量统一站点结构无需改动任何书籍配置。
- `.nojekyll` 是写给 GitHub Pages 的零字节开关，跳过 Jekyll 处理；因为输出目录每次都被删除重建，它必须在构建收尾时由代码重新写入。
- `build` 与 `deploy` 的全部差异是输出目录名（`site/` vs `docs/`）加一行提示；`site/` 由 `cmd_serve` 消费（本地预览），`docs/` 由 pages.yml 的 `upload-pages-artifact` 消费（线上发布），两者都通过 `.gitignore` 排除在版本库之外。

## 7. 下一步学习建议

`build_to` 的收尾调用了 `write_landing_page(&out)`——我们本讲刻意把它当作黑盒。下一讲 **u2-l4（write_landing_page：统一落地页生成）** 将拆开它：`BOOKS` 元组表如何被 `format!` 与迭代器链渲染成带分类色彩的 HTML 卡片，以及 `category_label` 的映射兜底逻辑。如果你更关心 `site/` 的下游，可以先跳到 **u2-l5（内置静态服务器 I：路径解析与多层安全防护）**，看 `cmd_serve` 如何把这个静态目录安全地暴露在 3000 端口；想了解 `docs/` 的完整上线流程，则进入 **u4-l1（GitHub Pages 自动部署流水线）** 逐段精读 pages.yml。
