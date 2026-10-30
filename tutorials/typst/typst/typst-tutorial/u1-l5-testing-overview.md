# 测试体系入门：cargo testit 与 tests/ 目录

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `tests/` 目录的 `src`、`suite`、`ref`、`store` 四层结构各自存放什么。
2. 熟练使用 `cargo testit` 的按名过滤（正则）、按路径过滤（`-p`）、精确匹配（`--exact`）、`--stages`、`--list` 与 `--update`。
3. 看懂套件中的三类测试：断言型（`#test`）、诊断型（`// Error: ...` 注解）、视觉对照型（参考图片/文件）。
4. 理解「文件引用」与「哈希引用」两种对照策略的差异，以及 `--update` 更新引用的完整流程。
5. 能够独立为 Typst 写一个新测试并让它跑通——这是后续阅读和修改编译器源码时「改了代码如何验证」的基本功。

## 2. 前置知识

本讲需要一点背景概念，我们都用通俗方式解释：

- **单元测试与集成测试**：Rust 中单元测试写在各 crate 内部（`#[cfg(test)]` 模块），集成测试放在独立目录、把项目当黑盒使用。Typst 的 `tests/` 是后者——它把「编译一份 `.typ` 文档」整个跑一遍来验证行为。
- **自定义测试框架（`harness = false`）**：Cargo 默认用 Rust 自带的 libtest 框架跑测试。Typst 关掉了它（后面会看到具体配置），自己实现了一个测试运行器，因此能用 `--- name attr ---` 这种 Typst 专属语法组织测试。
- **cargo 别名（alias）**：在 `.cargo/config.toml` 里可以给一长串命令起个短名字，比如 `testit`。
- **正则表达式过滤**：和 `cargo test` 一样，测试可以按名字的正则模式筛选，例如 `^math` 表示「以 math 开头」。
- **哈希（hash）**：把任意大的数据压缩成固定长度摘要的函数。同样的输入必然得到同样的哈希；哈希不同则内容一定不同。Typst 用它来避免把大文件提交进仓库。
- **「阶段 / stage」**：一份测试源码从求值到产出各种格式，中间要经过几个环节。Typst 把这些环节叫 stage，例如 `eval`（求值）、`paged`（分页编译）、`pdf`（产出 PDF）。

另外，本讲承接 u1-l3 的结论：**架构文档可能滞后，以源码为准**——本讲会再次遇到这个现象（README 里的属性列表与当前代码不一致）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tests/README.md](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md) | 测试套件的官方说明文档，本讲的主线 |
| [.cargo/config.toml](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/.cargo/config.toml) | 定义 `cargo testit` / `cargo docit` 别名 |
| [tests/Cargo.toml](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/Cargo.toml) | 测试包 `typst-tests` 的清单，声明 `harness = false` |
| [tests/src/tests.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/tests.rs) | 测试运行器入口：模块划分、路径常量、命令分发 |
| [tests/src/args.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/args.rs) | 运行器的命令行参数定义（clap） |
| [tests/src/collect.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs) | 遍历 `suite` 收集测试、解析 `--- name attr ---` 头、定义 stage 树 |
| [tests/src/notes.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/notes.rs) | 解析 `// Error:` 等诊断注解 |
| [tests/src/world.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs) | `TestWorld`：给编译器提供输入的环境，并注入 `test` 等辅助函数 |
| [tests/src/run.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/run.rs) | 执行单个测试：按 stage 顺序编译并对照引用 |
| [tests/src/output.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/output.rs) | 各输出格式的生成、哈希与对照逻辑 |
| [tests/wrapper/src/main.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/wrapper/src/main.rs) | `test-wrapper` 包：`cargo testit` 实际启动的程序 |
| [tests/suite/foundations/array.typ](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/foundations/array.typ) | 真实套件文件，包含三类测试的实例 |
| [tests/ref/pdf/hashes.txt](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/ref/pdf/hashes.txt) | 哈希引用的真实样例 |

## 4. 核心概念与源码讲解

### 4.1 tests/ 目录结构：四层布局与 cargo testit

#### 4.1.1 概念说明

Typst 的测试不是散落在各 crate 里的零散断言，而是一个**独立的集成测试包**，把「一段 `.typ` 源码 → 编译 → 各种输出」的完整流程当作被测对象。仓库根目录的 `tests/` 分四层：

| 目录 | 存放内容 | 是否提交进 git |
| --- | --- | --- |
| `tests/src` | 测试运行器的 Rust 代码 | 是 |
| `tests/suite` | 输入文件（`.typ`），按模块分目录组织 | 是 |
| `tests/ref` | 期望输出（参考图片 / 参考文件 / 哈希表） | 是 |
| `tests/store` | 测试跑出来的「鲜活输出」与报告 | 否（被 gitignore） |

这套设计的核心思想是**黄金主文件测试（golden file testing）**：先把「正确输出」固化在 `ref` 里，每次改动后重跑，拿新输出和旧引用比对，不一致就报失败。

#### 4.1.2 核心流程

一次 `cargo testit` 的执行链路：

```text
cargo testit <参数>
  └─ (别名展开) cargo run --quiet --package=test-wrapper -- <参数>
       └─ test-wrapper 转发: cargo test --workspace --test=tests -- <参数>
            └─ 运行 tests/src/tests.rs 的 main()
                 1. collect: 递归扫描 tests/suite/**/*.typ，解析 --- name attr --- 分节
                 2. 读取 tests/ref 中的引用（含 hashes.txt）
                 3. 并行（rayon）逐个运行测试：按 stage 编译并对照引用
                 4. 失败时生成 tests/store/report.html
```

#### 4.1.3 源码精读

**目录结构的官方定义**在 README 开头：

> [tests/README.md:L1-L11](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L1-L11) —— 定义 `src`（测试代码）、`suite`（输入文件，每个文件可含多个测试，每个测试是 `--- {name} {attr}+ ---` 后面的一段 Typst 代码）、`ref`（对照用参考）、`store`（鲜活输出）四层。

**测试包用自定义 harness**，这是整个体系的技术前提：

> [tests/Cargo.toml:L9-L12](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/Cargo.toml#L9-L12) —— 声明名为 `tests` 的测试目标指向 `src/tests.rs`，并设置 `harness = false`，即不用 libtest，而是运行 `tests.rs` 里自己写的 `main`。

**`cargo testit` 是个别名**，本体是先启动 `test-wrapper`：

> [.cargo/config.toml:L2-L8](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/.cargo/config.toml#L2-L8) —— 注释说明 wrapper 存在的意义：当测试产物需要重新生成时，支持在另一个 commit 上重跑测试运行器本身；第 7 行就是别名定义 `testit = "run --quiet --package=test-wrapper --"`。

> [tests/wrapper/src/main.rs:L22-L31](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/wrapper/src/main.rs#L22-L31) —— wrapper 把参数原样转发给真正的命令 `cargo test --workspace --test=tests -- <参数>`。所以 `cargo testit xxx` 与 `cargo test --workspace --test=tests -- xxx` 完全等价，`testit` 只是省了敲字。

**运行器入口**的模块划分与路径常量：

> [tests/src/tests.rs:L3-L13](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/tests.rs#L3-L13) —— 运行器由 `args`（参数）、`collect`（收集）、`notes`（注解）、`run`（执行）、`world`（环境）等 11 个模块组成。

> [tests/src/tests.rs:L33-L49](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/tests.rs#L33-L49) —— 定义了 `SUITE_PATH`、`STORE_PATH`、`REF_PATH` 等路径常量，以及 `REF_LIMIT = 20 * 1024`（参考输出默认不超过 20 KiB）。

> [tests/src/tests.rs:L51-L61](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/tests.rs#L51-L61) —— `main` 按 ARGS 分发：无子命令则跑测试，`clean` 清空 store，`undangle` 删除悬空引用等。

> [tests/src/tests.rs:L70-L73](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/tests.rs#L70-L73) —— 启动时在 `tests/store` 下创建 `render`、`html`、`pdf`、`pdftags`、`svg`、`bundle`、`by-hash` 子目录。

**过滤参数的定义**：

> [tests/src/args.rs:L17-L25](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/args.rs#L17-L25) —— `pattern` 是一组正则（多个之间是「或」关系）；`-p/--path` 把选择范围限制在给定路径；`--exact` 只选与名字**逐字**匹配的那一个测试。

> [tests/src/args.rs:L26-L31](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/args.rs#L26-L31) —— `--list` 只列出将要运行的测试不实际执行；`-u/--update` 更新未通过测试的引用输出。

README 中的用法速查（可直接照抄）：

> [tests/README.md:L27-L55](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L27-L55) —— 按名过滤（`cargo testit math`、`cargo testit "^math" "^page"`）、按路径过滤（`cargo testit -p tests/suite/model -p tests/suite/text`）、组合使用（`cargo testit "^issue" -p tests/suite/model`）以及精确执行（`cargo testit --exact math-attach-mixed`）。

补充一个 README 未展开的实用点：套件是按代码结构平行组织的，例如 `tests/suite/foundations/` 对应 foundations 模块、`tests/suite/math/` 对应数学。此外还有 `tests/packages/` 存放测试专用包，由 `TestFiles` 解析：

> [tests/src/world.rs:L157-L166](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L157-L166) —— 把 `@test` 命名空间的包解析到 `tests/packages/{name}-{version}` 目录。

#### 4.1.4 代码实践

1. **实践目标**：建立对四层目录的直观认识，并验证 `--list` 过滤能力。
2. **操作步骤**：
   ```bash
   # 在仓库根目录
   ls tests/                 # 应看到 src suite ref store（以及 wrapper、packages 等）
   find tests/suite -name '*.typ' | head    # 感受套件规模与目录组织
   cargo testit --list | tail -n 3           # 列出全部测试（数量很大，看末尾统计行）
   cargo testit --list "^array-" | head      # 只列出名字以 array- 开头的测试
   cargo testit --list -p tests/suite/math | head
   ```
3. **需要观察的现象**：`--list` 会逐行打印 `测试名 (文件:行号)`，最后输出一行 `N selected, M skipped` 统计。
4. **预期结果**：`-p tests/suite/math` 列出的测试全部来自 `tests/suite/math/` 下的文件；`^array-` 列出的测试名都以 `array-` 开头（如 `array-basic-syntax`，其位置指向 `tests/suite/foundations/array.typ`）。具体数量随版本变化，待本地验证。
5. 首次运行会编译 `test-wrapper` 及整套测试运行器，耗时数分钟属正常现象。

#### 4.1.5 小练习与答案

**练习 1**：`cargo testit math page` 和 `cargo testit "^math" "^page"` 有什么区别？

**答案**：前者是**一条**正则 `math page`，要求名字中连续出现字符串 "math page"（基本匹配不到任何测试）；后者是**两条**独立正则，名字含 "math" **或** 以 "page" 开头的测试都会被选中。多个 pattern 参数之间是「或」的关系，见 [tests/src/args.rs:L17-L19](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/args.rs#L17-L19) 与 [tests/README.md:L30-L33](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L30-L33)。

**练习 2**：为什么需要 `test-wrapper` 这个中间层，而不是让别名直接指向 `cargo test --workspace --test=tests`？

**答案**：因为哈希引用只存哈希不存文件（见 4.4 节），当测试失败需要生成缺失的「旧鲜活输出」时，必须在**旧版本代码**上重跑测试。wrapper 负责拦截特定退出码、询问用户并用 `git read-tree` 切到旧 revision 重跑，见 [tests/wrapper/src/main.rs:L8-L9](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/wrapper/src/main.rs#L8-L9) 与 [.cargo/config.toml:L2-L6](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/.cargo/config.toml#L2-L6)。

**练习 3**：`tests/store` 里的东西为什么可以随便删？

**答案**：它只存放测试运行时产生的「鲜活输出」与 HTML 报告，不入库；删除后重跑测试即可重建（`cargo testit clean` 就是干这个的，见 [tests/src/args.rs:L140-L143](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/args.rs#L140-L143)）。

### 4.2 测试语法与属性：`--- name attr+ ---` 与三类测试

#### 4.2.1 概念说明

一个套件文件（`.typ`）里可以塞**多个测试**，每个测试由一行「头部」和一段正文组成：

```typ
--- 测试名 属性1 属性2 ---
// 正文：一段会被真实编译的 Typst 代码
```

规则要点：

- 测试名必须**全套件唯一**（方便跨文件迁移），且是合法的 Typst 标识符（可含连字符，如 `math-attach-mixed`）。
- 头部必须至少声明一个目标属性（如 `eval`、`paged`、`html`）。
- 文件中第一个 `---` 之前只能是注释或空行（称为 preamble）。
- README 把套件里的测试归纳为三类：**断言型**（代码运行成功且断言成立）、**诊断型**（代码恰好产生指定的错误/警告消息）、**视觉对照型**（产出与参考文件一致的输出）。

#### 4.2.2 核心流程

收集器解析一个套件文件的伪代码：

```text
for 每个以 --- 开头的分节:
    解析名字与属性（校验：合法标识符、无重名、至少一个 stage、eval 不能与其他 stage 并用）
    正文 = 从头部下一行到下一个 ---（或文件末尾）
    从正文中抽取 // Error: / // Warning: / // Hint: / // Trace: 注解
    若名字/路径被 --exact / -p / pattern 过滤掉 → 计入 skipped，不运行
    否则加入待运行列表
```

#### 4.2.3 源码精读

**语法总则**（官方描述）：

> [tests/README.md:L128-L135](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L128-L135) —— `--- {name} {attr}+ ---` 语法；名字必须全局唯一；至少声明一个目标，例如 `--- my-test html ---`。

**解析器实现**（`Parser::parse`）：

> [tests/src/collect.rs:L616-L636](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L616-L636) —— 吃掉 `---` 后读取名字，校验非空、`is_ident` 合法性，然后交给 `parse_attrs` 解析属性。

> [tests/src/collect.rs:L638-L640](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L638-L640) —— 用 `seen` 哈希表检查重名，重名直接报 `duplicate test` 错误——这就是「名字全局唯一」的强制手段。

> [tests/src/collect.rs:L655-L667](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L655-L667) —— 正文一直读到下一个 `---`；随后按命令行参数（`--exact`/`-p`/pattern）过滤，不匹配的计入 `skipped`。

**属性解析与校验**（`parse_attrs`）：

> [tests/src/collect.rs:L710-L739](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L710-L739) —— 一个 `match` 罗列了当前支持的全部属性：`eval`、`paged`、`pdf`、`pdftags`、`html`、`bundle`（stage 类），`tolerance(n)`（图像比对容差）、`features(...)`（特性开关，带参数），`large`、`empty`、`trace`（布尔标记）。未知属性直接报错。

> [tests/src/collect.rs:L756-L769](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L756-L769) —— 两条硬校验：至少声明一个 stage；`eval` 必须是唯一 stage（且配 `empty` 是冗余的）。

⚠️ **文档与代码的差异**（呼应 u1-l3「以源码为准」）：[tests/README.md:L137-L149](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L137-L149) 列出的属性中有 `pdfstandard({standard})`，但在当前 HEAD 的 `tests/src` 中已检索不到它的处理代码；反过来，代码支持的 `bundle`、`tolerance(n)`、`features(...)` 尚未写进 README。**以 `parse_attrs` 的 match 为准。**

**三类测试的真实样例**（同一个文件里三态俱全）：

> [tests/suite/foundations/array.typ:L68-L71](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/foundations/array.typ#L68-L71) —— **断言型**：`--- array-len eval ---` 用 `#test(().len(), 0)` 断言数组方法行为。

> [tests/suite/foundations/array.typ:L23-L26](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/foundations/array.typ#L23-L26) —— **诊断型**：`--- array-bad-token eval ---` 用 `// Error: 4-6 ...` 与 `// Hint: 4-6 ...` 注解声明「第 1 个非注解行的第 4-6 列应报什么错、给什么提示」。

> [tests/suite/foundations/array.typ:L3-L21](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/foundations/array.typ#L3-L21) —— **视觉对照型**：`--- array-basic-syntax paged ---` 展示各种数组字面量，其渲染结果要与 `tests/ref/render/` 中的参考图片逐像素对照。

**诊断注解的数据模型**：

> [tests/src/notes.rs:L172-L199](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/notes.rs#L172-L199) —— `Note` 结构保存一条注解（种类 + 范围 + 消息）；`NoteKind` 枚举就是 `Error` / `Warning` / `Hint` / `Trace` 四种。README 对注解写法的说明见 [tests/README.md:L180-L197](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L180-L197)（含跨行范围 `3:2-3:7` 的写法与 `trace` 属性）。

**`#test` 是哪来的？** 它不是 Typst 标准库函数，而是测试运行器注入的：

> [tests/src/world.rs:L224-L236](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L224-L236) —— 构建「扩展版标准库」时向全局作用域注入 `test`、`test-repr`、`print`、`lines`、`bounds` 等辅助函数，以及 `conifer`/`forest` 两个测试常用颜色。

> [tests/src/world.rs:L261-L266](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L261-L266) —— `test` 的实现就是比较两个 `Value`，不相等就抛出 `Assertion failed: ... != ...`。所以在普通文档里没有 `test`，只有标准库的 `assert`（README 提到两者非常相似，`test` 只是更短，见 [tests/README.md:L161-L163](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L161-L163)）。

顺带一提，测试世界的默认排版参数也被刻意调成「整数友好」：

> [tests/src/world.rs:L211-L245](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs#L211-L245) —— 页宽固定 120pt、四边 10pt 边距（版心恰好 100pt）、字号 10pt、页高不限，注释里明说是为了让数值「乘出好看的整数」。

#### 4.2.4 代码实践

1. **实践目标**：亲眼确认三类测试的判定行为。
2. **操作步骤**：
   ```bash
   cargo testit --exact array-len        # 断言型
   cargo testit --exact array-bad-token  # 诊断型
   cargo testit --exact array-basic-syntax   # 视觉对照型
   ```
3. **需要观察的现象**：测试名旁的通过/失败标记；诊断型测试若故意注解错误会显示期望 vs 实际消息差异。
4. **预期结果**：三个测试都通过（它们是仓库现存的合格测试）。然后做一个破坏实验：临时把 `tests/suite/foundations/array.typ` 中 `array-len` 的断言改成 `#test(().len(), 1)`，重跑 `cargo testit --exact array-len` 应失败并显示 `Assertion failed: 0 != 1` 之类的消息（具体措辞待本地验证）；验证完用 `git restore tests/suite/foundations/array.typ` 还原。
5. 不要提交这个破坏性改动。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `eval` 属性不允许与 `paged`、`html` 同时出现？

**答案**：`eval` 测试只关心求值行为且**必须产出空内容**；而带输出目标的测试语义不同。混用会让「该不该生成引用、内容是否该为空」变得含糊，所以 [collect.rs:L760-L769](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L760-L769) 直接禁止，并提示「`eval` 必须是唯一 stage」。

**练习 2**：断言型测试什么时候该用 `eval`，什么时候必须换成 `paged empty` / `html empty`？

**答案**：如果断言只涉及脚本执行（纯求值），用 `eval`。如果测试逻辑依赖 show 规则等**排版期才会执行**的代码，就得真的编译，但又没有可见输出需要对照，此时用 `paged empty`/`html empty`：编译会跑完整管线，同时 `empty` 声明「不应产生非平凡输出」。README 的说明见 [tests/README.md:L164-L169](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L164-L169)。

**练习 3**：`empty` 属性是怎么被强制的？

**答案**：运行器会反向检查。带 `eval` 的测试若产出了非空内容，或声明了 `empty` 的测试产出了非空输出，都会失败并给出提示（如「hint: consider making this a `paged empty` or `html empty` test」「hint: consider removing the `empty` attribute」），逻辑在 [tests/src/run.rs:L142-L163](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/run.rs#L142-L163) 与 [tests/src/run.rs:L215-L261](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/run.rs#L215-L261)。

### 4.3 stage 树：从 eval 到各输出格式的执行顺序

#### 4.3.1 概念说明

「stage」把测试的执行拆成一棵树：根是求值（`eval`），再分叉到编译目标（`paged` 分页目标、`html` 目标、实验性 `bundle` 目标），每个目标下挂具体输出（`render`/`pdf`/`svg` 等）。理解这棵树你才能回答：改一行代码后，哪些测试会被哪些输出格式检验？`--stages` 该怎么选来加速？

#### 4.3.2 核心流程

当前代码中的 stage 树（注意比 README 多了 `bundle` 分支）：

```text
                  ╭─> render（PNG，文件引用）
       ╭─> paged ─┼─> pdf ───> pdftags
eval ─┤          ╰─> svg
      ├─> html  ────> html
      ╰─> bundle ───> bundle（tar，文件引用）
```

两条推导规则：

- **蕴含（implied）**：声明 `paged` 等于同时要测 `render`、`pdf`、`svg`。
- **前置（required）**：要测 `pdf` 必须先跑 `paged`（和 `eval`）；要测 `pdftags` 必须先产出 `pdf`。

`--stages` 参数与测试自身属性取交集，决定这个测试实际跑哪些环节。

#### 4.3.3 源码精读

**stage 的位标志定义与树形注释**：

> [tests/src/collect.rs:L127-L150](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L127-L150) —— `TestStages` 用 8 个 bit 表示 `EVAL/PAGED/RENDER/PDF/PDFTAGS/SVG/HTML/BUNDLE`，doc 注释里画的就是上面那棵树。README 中的树少了 bundle 分支，见 [tests/README.md:L72-L79](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L72-L79)——又一个文档滞后的小例子。

**蕴含规则**：

> [tests/src/collect.rs:L152-L172](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L152-L172) —— `with_implied`：`PAGED` 会并入 `RENDER | PDF | SVG`；其余 stage 无额外蕴含。

**前置规则**：

> [tests/src/collect.rs:L174-L194](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L174-L194) —— `with_required`：`RENDER/PDF/SVG` 都要求 `EVAL | PAGED`；`PDFTAGS` 额外要求 `PDF`；`HTML`/`BUNDLE` 只要求 `EVAL`。

**CLI 侧的 stage 计算**：

> [tests/src/args.rs:L98-L129](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/args.rs#L98-L129) —— 不传 `--stages` 时默认全部 stage；传入时取并集，再依次套用 `with_implied` 与 `with_required`（注释强调顺序必须是先 implied 后 required）。

**单个测试的实际执行顺序**（`Runner::run`，本模块最重要的代码）：

> [tests/src/run.rs:L142-L163](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/run.rs#L142-L163) —— **无条件先求值**（结果会被缓存，后续编译不重复求值），并根据 `eval` 属性与 `empty` 语义做正反两方向的空内容检查。

> [tests/src/run.rs:L165-L195](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/run.rs#L165-L195) —— 需要时编译 `PagedDocument`，依次执行：`render`（文件引用对照）→ `svg`（哈希对照）→ `pdf`（哈希对照）→ 若要 `pdftags` 则基于刚产出的 PDF 再做标签树对照。注意 `pdftags` 复用了 `pdf` 阶段的产物。

> [tests/src/run.rs:L197-L207](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/run.rs#L197-L207) —— `html` 目标编译 `HtmlDocument` 后做哈希对照；`bundle` 目标编译 `Bundle` 后做文件对照。

**用 `--stages` 加速**：

> [tests/README.md:L57-L84](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L57-L84) —— 默认跑全部 stage（含 PDF/SVG 生成，较慢）；开发时可用 `--stages html,pdftags` 这类逗号分隔列表只跑部分目标。

#### 4.3.4 代码实践

1. **实践目标**：用运行时间差异印证 stage 裁剪的效果。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>
   time cargo testit -p tests/suite/math/attach.typ --stages eval
   time cargo testit -p tests/suite/math/attach.typ
   ```
3. **需要观察的现象**：第二次运行除了求值还会编译分页文档并生成 render/svg/pdf 三种输出，应明显慢于第一次；`tests/store/render/` 等目录会出现（或更新）对应输出文件。
4. **预期结果**：全 stage 耗时显著大于 `--stages eval`。具体倍数依赖机器，待本地验证。
5. 想只重跑单个 stage 的引用更新也可以组合参数：`cargo testit --exact math-attach-mixed --update --stages=render`（README 示例见 [tests/README.md:L238-L242](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L238-L242)）。

#### 4.3.5 小练习与答案

**练习 1**：一个测试头部写的是 `--- foo pdftags ---`，实际会执行哪些 stage？

**答案**：显式声明的只有 `pdftags`，但按 `with_required` 规则它要求 `PDF`，`PDF` 又要求 `PAGED`，`PAGED` 要求 `EVAL`；同时 `with_implied` 会让 `PAGED` 蕴含 `RENDER|PDF|SVG`。对照逻辑上主要校验 `pdftags` 输出（以及被要求跑通的环节）。依据：[collect.rs:L174-L194](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L174-L194)。

**练习 2**：为什么 `Runner::run` 每次都先求值，即使测试属性里没有 `eval`？

**答案**：求值是所有编译目标共同的输入，且结果被缓存复用；同时运行器需要它来做「空/非空内容」的健康检查（比如发现一个 `paged` 测试其实啥也没产出），见 [run.rs:L142-L157](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/run.rs#L142-L157) 的注释与逻辑。

**练习 3**：`pdftags` 为什么不独立编译一份 PDF？

**答案**：标签树是从 PDF 导出过程中提取的，`run` 中 `pdf` 阶段产出的 PDF 直接传给 `pdftags` 检查（[run.rs:L189-L194](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/run.rs#L189-L194)），既省时间也保证两者检查的是同一份数据。

### 4.4 引用更新与测试报告：ref、store、哈希与 --update

#### 4.4.1 概念说明

「对照」是测试的核心，Typst 有两种引用策略：

| 策略 | 存储形式 | 用于哪些输出 | 优缺点 |
| --- | --- | --- | --- |
| **文件引用** | `ref/{format}/{name}.{ext}` 整个文件入库 | `render`（PNG）、`bundle` | 直观、可 review；占空间，有 20 KiB 上限 |
| **哈希引用** | `ref/{format}/hashes.txt` 中一行 `<32位十六进制哈希> <测试名>` | `pdf`、`pdftags`、`svg`、`html` | 每条固定 32 字符，仓库不膨胀；但哈希不匹配时看不出「变了什么」，需要借助 store 里的鲜活输出与报告 |

测试运行时，所有格式的「活输出」都写进 `tests/store`；哈希引用的输出按 `{hash}_{name}.{ext}` 存到 `store/by-hash/`，并在 `store/{format}/` 下建同名软链方便浏览。

#### 4.4.2 核心流程

对照与更新流程：

```text
运行测试 → 产出活输出（live）
  ├─ 文件引用输出：live 压缩/规范化后与 ref 文件逐字节比对（render 允许极小容差）
  └─ 哈希引用输出：对 live 做稳定哈希（128bit）与 hashes.txt 中该测试的哈希比对
失败 or --update:
  ├─ 文件引用：把 live 保存为新的 ref 文件（PNG 用 oxipng 最大压缩）
  ├─ 哈希引用：更新内存哈希表，跑完统一排序后重写 hashes.txt
  └─ 诊断注解：也可被 --update 改写回测试源文件（Note 的 Updated 状态）
有失败 → 生成自包含 HTML 报告 tests/store/report.html（含文本/图像 diff）
```

#### 4.4.3 源码精读

**两种策略的官方说明**：

> [tests/README.md:L95-L98](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L95-L98) —— 文件引用：期望输出直接存 `ref` 目录并入库。

> [tests/README.md:L100-L115](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L100-L115) —— 哈希引用：只存 32 字节哈希防仓库膨胀；活输出按哈希命名存 `store/by-hash`，并在 `store/{format}/` 建软链以便人工查看。

**哪种输出用哪种策略**（`TestOutput::kind`）：

> [tests/src/collect.rs:L389-L398](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L389-L398) —— `render` 与 `bundle` 是文件引用；`pdf`、`pdftags`、`svg`、`html` 是哈希引用。配套常量 `HASH_OUTPUTS` 见 [tests/src/output.rs:L173-L178](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/output.rs#L173-L178)。

**hashes.txt 的格式**：

> [tests/src/output.rs:L87-L94](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/output.rs#L87-L94) —— 序列化时每行写成 `{hash} {name}`；解析见同文件 [L41-L54](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/output.rs#L41-L54)。

> [tests/src/output.rs:L105-L126](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/output.rs#L105-L126) —— 哈希本体是 `u128`，展示成 32 个十六进制字符（128 bit / 4）。

真实样例（每行 = 哈希 + 测试名）：

> [tests/ref/pdf/hashes.txt:L1-L6](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/ref/pdf/hashes.txt#L1-L6) —— 例如 `3c5e3609bbae8b694e2abcdfe8c99fef array-basic-syntax` 正是 4.2 节那个视觉测试的 PDF 哈希。

**store 路径规则**：

> [tests/src/collect.rs:L360-L374](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L360-L374) —— `hash_path` 生成 `store/by-hash/{hash}_{name}.{ext}`；`live_path` 生成 `store/{format}/{name}.{ext}`（软链指向前者）。

**20 KiB 体积红线**：

> [tests/src/collect.rs:L510-L540](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L510-L540) —— 收集阶段逐个检查 ref 文件：超过 `REF_LIMIT`（20 KiB，定义在 [tests/src/tests.rs:L48-L49](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/tests.rs#L48-L49)）且未标 `large` 的引用直接算解析错误。同时这里还检查「悬空引用」：ref 里有文件却没有对应测试。

**--update 的落地**：

> [tests/src/args.rs:L29-L31](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/args.rs#L29-L31) —— `-u/--update`：更新未通过测试的引用输出。

> [tests/src/run.rs:L31-L42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/run.rs#L31-L42) —— `update_hash_refs`：全部测试跑完后，把更新过的哈希表排序并整写回 `ref/{format}/hashes.txt`。

> [tests/src/output.rs:L222-L227](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/output.rs#L222-L227) —— render 引用保存时用 `oxipng` 最大压缩重新编码 PNG——这就是 README「更新视觉测试一般会生成压缩过的参考图」的实现。

> [tests/README.md:L231-L246](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L231-L246) —— 官方更新流程：`cargo testit --exact my-test-name --update`，可叠加 `--stages` 只更新部分引用。

**缺失旧活输出与 regen**：

> [tests/README.md:L117-L123](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L117-L123) —— 哈希不匹配但 store 里缺旧活输出时，wrapper 会询问是否生成：切到引用哈希被提交的那个 revision 重跑测试；手动等价命令是 `cargo testit regen`。

> [tests/wrapper/src/main.rs:L93-L154](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/wrapper/src/main.rs#L93-L154) —— `regen` 的实现：要求工作树干净 → 用 `git read-tree` 依次检出最多 3 个旧 revision 跑测试，直到补齐缺失文件 → 回到 HEAD 重跑。这就是「test-wrapper 存在的意义」的完整答案。

**测试报告**：

> [tests/README.md:L86-L90](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L86-L90) —— 有失败时默认生成自包含 HTML 报告到 `tests/store/report.html`，可用 `--no-report` 关闭、`--open-report` 自动打开；参数定义见 [tests/src/args.rs:L56-L61](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/args.rs#L56-L61)。

#### 4.4.4 代码实践

1. **实践目标**：观察一次「失败 → 报告 → 更新 → 通过」的完整循环。
2. **操作步骤**：
   ```bash
   # a) 挑一个现存视觉测试，人为制造失败：临时改掉它的参考图一个字节
   cp tests/ref/render/array-basic-syntax.png /tmp/ref-backup.png
   printf 'x' >> tests/ref/render/array-basic-syntax.png
   cargo testit --exact array-basic-syntax        # 应失败

   # b) 打开报告查看图像 diff
   cargo testit open-report        # 或手动用浏览器打开 tests/store/report.html

   # c) 用 --update 修复（从活输出重新生成参考）
   cargo testit --exact array-basic-syntax --update --stages=render
   cargo testit --exact array-basic-syntax        # 再次运行应通过
   ```
3. **需要观察的现象**：失败信息指出 render 对照不一致；报告里有新旧图像并排 diff；更新后 `git diff tests/ref/render/array-basic-syntax.png` 会显示二进制变化（若与原文件不同，说明你追加的字节已被正确覆盖——内容哈希应与备份一致，可用 `cmp /tmp/ref-backup.png tests/ref/render/array-basic-syntax.png` 验证）。
4. **预期结果**：c) 步之后测试通过且 `cmp` 无输出（更新把参考恢复为正确内容）；同时注意哈希引用的测试失败时只报哈希不同，具体差异要去 `tests/store/{format}/` 看活输出或看报告。本实验输出细节待本地验证。
5. **收尾**：`git restore tests/ref`（或 `cp /tmp/ref-backup.png tests/ref/render/array-basic-syntax.png`）把仓库恢复原状，勿提交实验改动。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `render` 用文件引用而 `pdf`/`svg`/`html` 用哈希引用？

**答案**：PNG 参考图需要人工肉眼 review 排版效果，必须以文件形式存在；PDF/SVG/HTML 体量大且不需要逐字节人工审阅，只判断「变没变」即可，哈希每条固定 32 字符可防仓库膨胀。分工见 [collect.rs:L389-L398](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L389-L398)，动机说明见 [tests/README.md:L100-L106](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L100-L106)。

**练习 2**：删掉 `tests/suite/math/attach.typ` 里的一个测试段后，对应的 ref 文件会怎样？

**答案**：收集阶段会发现「ref 里有文件/哈希但没有对应测试」，报告为悬空引用（dangling）错误（[collect.rs:L483-L508](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/collect.rs#L483-L508)），可用 `cargo testit undangle` 清理（子命令定义见 [args.rs:L140-L145](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/args.rs#L140-L145)）。

**练习 3**：哈希不匹配时，如何知道输出到底变了什么？

**答案**：三个途径——看 `tests/store/{format}/{name}.{ext}` 的最新活输出（软链到 `by-hash`）；看 `tests/store/report.html` 的文本/图像 diff；若缺旧的活输出，让 wrapper 在旧 revision 上重跑生成（`cargo testit regen`），见 [tests/README.md:L107-L126](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L107-L126)。

## 5. 综合实践

**任务：给 `range` 函数写一个迷你测试文件，覆盖三类测试中的两类，并用 `--exact` 与 `--update` 走完整个生命周期。**（这就是本讲规格中指定的实践任务）

1. **准备**：先精确运行一个现存的 eval 测试，确认环境正常：
   ```bash
   cargo testit --exact array-len
   ```
   预期：1 个测试通过（位于 `tests/suite/foundations/array.typ:68`）。

2. **新建断言型测试**：创建文件 `tests/suite/foundations/tutorial-range.typ`（新文件，不动任何现有代码）：
   ```typ
   // Tutorial: tests for range().

   --- tutorial-range-basics eval ---
   #test(range(3), (0, 1, 2))
   #test(range(1, 8, step: 2), (1, 3, 5, 7))
   #test(type(range(2)), array)

   --- tutorial-range-bad-step eval ---
   // Error: 13-24 range must contain at least 1 step
   #range(1, 2, step: 0)
   ```
   注意：文件头只能写注释；测试名用 `tutorial-` 前缀避免与现有名称冲突（可先用 `cargo testit --list "^tutorial-"` 确认无重名）。诊断行的列号范围以你自己文件里的实际列为准，若消息或列号写错，运行器会把期望与实际一并打印出来供你修正。
3. **运行**：
   ```bash
   cargo testit --exact tutorial-range-basics
   cargo testit --exact tutorial-range-bad-step
   ```
   **说明**：这两个是 `eval` 测试，不产生任何引用输出，所以**不需要** `--update` 就能直接通过；`--update` 是给有参考输出的测试用的。
4. **扩展一个视觉测试并体验 `--update`**：在同一文件追加：
   ```typ
   --- tutorial-range-visual paged ---
   #range(5)
   ```
   先直接运行 `cargo testit --exact tutorial-range-visual`，预期失败（没有参考图）；再用 `--update` 生成引用：
   ```bash
   cargo testit --exact tutorial-range-visual --update
   cargo testit --exact tutorial-range-visual      # 现在应通过
   ```
   预期变化：新增 `tests/ref/render/tutorial-range-visual.png`（经压缩，小于 20 KiB），`tests/ref/pdf/hashes.txt` 与 `tests/ref/svg/hashes.txt` 各多出一行 `... tutorial-range-visual`（可用 `git diff tests/ref` 查看）。
5. **验证清单**：
   - [ ] 三个测试均通过；
   - [ ] `git status` 只显示新增的 `tests/suite/foundations/tutorial-range.typ` 与 `tests/ref` 下的变化；
   - [ ] `ls tests/store/render/ | grep tutorial` 能看到软链形式的活输出。
6. **清理**（练完恢复仓库原状）：
   ```bash
   rm tests/suite/foundations/tutorial-range.typ
   git restore tests/ref          # 撤销 --update 写入的引用
   cargo testit undangle          # 如仍提示悬空引用，用子命令清理
   ```
7. 全部命令的输出措辞与文件体积为**待本地验证**项；若 `--update` 生成的 PNG 超过 20 KiB（对本例不太可能），需加 `large` 属性或缩小内容。

## 6. 本讲小结

- `tests/` 按 `src`（运行器代码）、`suite`（输入）、`ref`（期望输出）、`store`（活输出与报告）四层组织；`cargo testit` 是 `.cargo/config.toml` 中的别名，经 `test-wrapper` 转发为 `cargo test --workspace --test=tests`。
- 测试以 `--- 名字 属性+ ---` 分节，名字全局唯一、至少一个目标属性；属性的真实清单以 `collect.rs::parse_attrs` 为准（README 略有滞后：`pdfstandard` 已不在代码中，代码另有 `bundle`、`tolerance(n)`、`features(...)`）。
- stage 树是 `eval → paged → render/pdf→pdftags/svg` 与 `eval → html`（外加实验性 `bundle`）；`with_implied`/`with_required` 两条规则决定实际执行集，`--stages` 可裁剪加速。
- 套件测试分三类：断言型（注入的 `#test`）、诊断型（`// Error: 4-6 ...` 注解）、视觉对照型（参考图/文件）；`eval` 测试必须产出空内容，`empty` 与之配合约束反向情况。
- 引用分两种：`render`/`bundle` 用文件引用（≤20 KiB，除非 `large`）；`pdf`/`pdftags`/`svg`/`html` 用 32 字符哈希引用（`ref/{format}/hashes.txt`），活输出按哈希存 `store/by-hash` 并建软链。
- `--update` 重写引用（PNG 经 oxipng 压缩，哈希表排序整写）；失败时生成 `tests/store/report.html`；缺失旧活输出时 `cargo testit regen` 会检出旧 revision 重跑补齐。

## 7. 下一步学习建议

- **下一讲 u2-l1（源码与文件身份：Source、FileId 与包路径）**：进入第二单元「语法层」。你会发现本讲的 `TestWorld`、`tests/packages` 解析都建立在 `Source`/`FileId` 之上。
- 两个衔接点值得留意：
  - 本讲的 `TestWorld`（[tests/src/world.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/world.rs)）是 `World` trait 的一个最小可用实现，是 u3-l1「World 抽象」的现成预习材料。
  - 运行器的 `--syntax` 与 `--parser-compare` 参数（[tests/src/args.rs:L72-L92](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/src/args.rs#L72-L92)）专为调试解析器设计，学完 u2-l3（Parser 与 SyntaxNode）后可以回来用它们把任意测试的语法树 dump 到 `tests/store/syntax/` 观察。
- 继续阅读建议：通读一遍 [tests/README.md](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md)，再挑一个你感兴趣模块的套件目录（如 `tests/suite/foundations/`）逐行读几个测试，体会「能用断言就不用参考图」的取舍（[tests/README.md:L227-L229](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/README.md#L227-L229)）。
