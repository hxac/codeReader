# 包的拉取与更新 fetch/update-package.sh

## 1. 本讲目标

本讲承接 [u2-l3 安装主循环](u2-l3-install-main-loop.md)，把镜头推进到 `install_package` 调用 `fetch_or_update_package` 之后的下一层：源码包到底是怎么从 GitHub 拉下来、又是怎么被更新到最新状态的。

学完后你应当能够：

- 说清 `fetch-package.sh` 用了哪些 `git clone` 选项，以及**为什么**要用这些选项（浅克隆、子模块、按 git 版本条件开启 `--shallow-submodules`）。
- 读懂 `update-package.sh` 里 `git_current_branch` / `git_default_branch` / `fetch_all_branches` / `switch_branch` 这一组辅助函数各自的职责。
- 复述「ff-only 合并失败 → 硬重置回退」这条核心容错路径，并解释为什么 plum 敢于直接 `reset --hard` 丢弃本地改动。
- 自己构造一次「本地分叉」，亲眼看到 update 流程把 `package/` 目录恢复成 origin 的状态。

## 2. 前置知识

本讲默认你已经掌握：

- **`package/` 是一次性缓存**：u2-l3 已说明，plum 把每个包浅克隆到 `${root_dir:-.}/package/<user>/<package>/`，这里**只是源文件的临时落脚点**，真正交付给用户的是随后被拷进 `rime_dir` 的文件。因此 `package/` 里的本地改动本就不被期望保留——这是本讲「硬重置」逻辑能成立的前提。
- **`fetch_or_update_package` 的二分判断**：在 [install-packages.sh:49-65](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L49-L65) 里，**目录不存在 → 调 `fetch-package.sh` 下载；目录已存在 → 调 `update-package.sh` 更新**。本讲就是在拆解这两个分支。
- **`no_update` 环境变量**：`install-packages.sh` 与 `update-package.sh` 都会读它（`${no_update:+1}`），置 1 后 update 阶段不发网络请求，仅报「Found package」。
- 若干 git 概念：浅克隆（shallow clone）、子模块（submodule）、远程跟踪分支（`origin/<branch>`）、`symbolic-ref`、fast-forward 合并、`reset --hard`。下面用到时还会再点一句。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [scripts/fetch-package.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/fetch-package.sh) | 54 | 「下载」分支：把一个包名解析成 GitHub URL，用一组浅克隆选项执行 `git clone`。无外部模块依赖，自成一体。 |
| [scripts/update-package.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh) | 96 | 「更新」分支：检测当前分支与目标分支，必要时切分支；同分支则 `fetch` + `ff-only merge`，失败则硬重置回 origin。依赖 `bootstrap.sh` + `styles`。 |
| [scripts/install-packages.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh)（调用方，已在 u2-l3 讲过） | — | 在 `fetch_or_update_package` 里按目录是否存在二选一调用上面两个脚本。 |

注意一个容易混淆的点：**`fetch-package.sh` 是作为独立子进程执行的**（见 [install-packages.sh:56](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L56) 的 `"${script_dir}"/fetch-package.sh ...`），它没有 `source bootstrap.sh`，因此它内部自己定义的 `resolve_package_name` 与 `resolver.sh` 里的同名函数**互不相干**——本讲 4.1 会专门区分两者。

## 4. 核心概念与源码讲解

### 4.1 浅克隆与子模块（fetch-package.sh）

#### 4.1.1 概念说明

`fetch-package.sh` 只做一件事：**把一个「包」从 GitHub 克隆到本地目录**。它的全部智慧集中在两点：

1. **包名 → GitHub URL**：用户给的可能是裸名 `prelude`、带前缀的 `rime-essay`，或完整的 `lotem/rime-zhung`，脚本要统一还原成 `https://github.com/<user>/<repo>.git`。
2. **克隆选项的取舍**：plum 不需要 git 历史，只需要「最新一份文件」，因此用了一组**省带宽、省时间**的浅克隆选项，并对老版本 git 做了兼容处理。

之所以敢只取最新文件，正是因为 `package/` 是一次性缓存（见第 2 节）——plum 后续靠 `install_files` 做增量拷贝，从不回看历史提交。

#### 4.1.2 核心流程

`fetch-package.sh` 的执行过程可以画成：

```
package_name="$1"; shift          # 取包名，剩下的参数原样留给 git clone
        │
        ▼
resolve_package_name(package_name) → "user/repo"
        │   ① 已含 "/"        → 原样（user/repo）
        │   ② 以 "rime-" 开头 → rime/<name>
        │   ③ 其余裸名        → rime/rime-<name>
        ▼
package_url = "https://github.com/<user/repo>.git"
        │
        ▼
git_version_greater_or_equal 2 9 ?  ──yes──▶ clone_options 追加 --shallow-submodules
        │ no
        ▼
git clone --depth 1 --recurse-submodules [--shallow-submodules] \
          <package_url>  "$@"        # "$@" = 目标目录 [+ --branch <branch>]
```

各选项含义：

- `--depth 1`：**浅克隆**，只拉取最新一次提交，不带历史。对一个只需「当前文件」的缓存来说足够。
- `--recurse-submodules`：同时初始化并克隆仓库的子模块（部分 Rime 数据包会把字典/语法作为子模块引用）。
- `--shallow-submodules`：让子模块也以 `--depth 1` 克隆。该选项在 **git ≥ 2.9** 才支持，所以脚本先做版本判断再加。

#### 4.1.3 源码精读

**URL 还原函数**（与 `resolver.sh` 的同名函数是两码事，这里负责「补全 GitHub 路径」）：

[scripts/fetch-package.sh:14-25](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/fetch-package.sh#L14-L25) 用三段 `=~` 正则判断输入形态，并拼出 URL：

```bash
resolve_package_name() {
    local name="$1"
    if [[ ${name} =~ [^/]*/[^/]* ]]; then   # 已是 user/repo
        echo ${name}
    elif [[ ${name} =~ rime-[^/]* ]]; then  # 已带 rime- 前缀
        echo rime/${name}
    else                                    # 裸名 → 补 rime/rime- 前缀
        echo rime/rime-${name}
    fi
}
package_url="https://github.com/$(resolve_package_name "${package_name}").git"
```

> 对照 `resolver.sh` 里的 `resolve_package_name`（[resolver.sh:19-24](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L19-L24)）：那个是**剥掉** `rime-` 前缀、用于给本地 `package/` 目录命名；而这个是**加上** `rime/rime-` 前缀、用于拼 GitHub URL。二者同名却反向，正是因为 `fetch-package.sh` 跑在独立子进程里，看不到 resolver 的定义。

**git 版本判断**（兼容老版本 git）：

[scripts/fetch-package.sh:27-40](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/fetch-package.sh#L27-L40) 解析 `git --version` 的主次版本号，判断是否 `>= target`：

```bash
git_version_greater_or_equal() {
    local target_major="$1"; local target_minor="$2"
    local git_version_pattern='^git version ([0-9]*)\.([0-9]*).*$'
    if [[ "$(git --version | grep '^git version')" =~ $git_version_pattern ]]; then
        local major="${BASH_REMATCH[1]}"; local minor="${BASH_REMATCH[2]}"
        [[ "${major}" -gt "${target_major}" ]] || (
            [[ "${major}" -eq "${target_major}" ]] && [[ "${minor}" -ge "${target_minor}" ])
    else
        return 1
    fi
}
```

判断式 `[[ major > t_major ]] || ( [[ major == t_major ]] && [[ minor >= t_minor ]] )` 读作「主版本更大，或主版本相等且次版本不低于目标」。后面那个 `( ... )` 是**子壳**，其退出状态就是内部 `&&` 链的结果，借此把「或」的右半支写成复合条件。

**按版本拼装 clone 选项并执行**：

[scripts/fetch-package.sh:42-53](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/fetch-package.sh#L42-L53) 维护一个数组 `clone_options`，条件追加后再展开给 `git clone`：

```bash
clone_options=( --depth 1 --recurse-submodules )
if git_version_greater_or_equal 2 9; then
    clone_options+=( --shallow-submodules )
fi
git clone ${clone_options[@]} "${package_url}" "$@"
```

末尾的 `"$@"` 是关键：脚本开头 `shift` 掉了包名，剩下的参数原封不动透传。结合调用方 [install-packages.sh:52-56](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L52-L56)，这里 `"$@"` 实际是 `"<package_dir>" [--branch "<branch>"]`——即目标目录，以及用户用 `@branch` 指定分支时附加的 `--branch`。

#### 4.1.4 代码实践

**目标**：验证 `resolve_package_name` 对三类输入的 URL 还原结果，**不需要联网**。

**步骤**：把函数体拷到一个临时 shell 里直接调用：

```bash
bash -c '
resolve_package_name() {
    local name="$1"
    if [[ ${name} =~ [^/]*/[^/]* ]]; then echo ${name}
    elif [[ ${name} =~ rime-[^/]* ]];   then echo rime/${name}
    else                                     echo rime/rime-${name}; fi
}
for n in prelude rime-essay lotem/rime-zhung; do
    printf "%-22s -> https://github.com/%s.git\n" "$n" "$(resolve_package_name "$n")"
done
'
```

**需要观察的现象**：三条输出分别是 `rime/rime-prelude`、`rime/rime-essay`、`lotem/rime-zhung`。

**预期结果**（据源码推断）：裸名 `prelude` 被补成 `rime/rime-prelude`；已带前缀的 `rime-essay` 被补成 `rime/rime-essay`；已是 `user/repo` 形态的 `lotem/rime-zhung` 原样保留。可对照本机 `git --version` 顺便判断本次 clone 会不会带 `--shallow-submodules`（≥2.9 则会）。实际克隆行为需联网，**待本地验证**。

#### 4.1.5 小练习与答案

1. **问**：为何 `fetch-package.sh` 不直接 `git clone` 整个仓库，而要用 `--depth 1`？
   **答**：因为 `package/` 只作为源文件的临时缓存，plum 后续只读最新一份文件做增量拷贝，从不查阅历史；浅克隆能显著省带宽和时间。

2. **问**：脚本里 `clone_options+=( --shallow-submodules )` 为什么要用 `git_version_greater_or_equal 2 9` 守卫？
   **答**：`--shallow-submodules` 是 git 2.9 才引入的选项，低版本 git 不识别会直接报错；先判版本可保证对老 git 的兼容。

3. **问**：若用户运行 `bash rime-install lotem/rime-zhung@master`，最终 `git clone` 命令大致长什么样？
   **答**：`git clone --depth 1 --recurse-submodules [--shallow-submodules] https://github.com/lotem/rime-zhung.git <package_dir> --branch master`（`@master` 经 resolver 拆成 `--branch master`，由 [install-packages.sh:52-56](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L52-L56) 透传到末尾的 `"$@"`）。

---

### 4.2 分支检测（git_current_branch / git_default_branch）

#### 4.2.1 概念说明

包一旦克隆过一次（`package/` 目录已存在），后续再装就走 `update-package.sh`。更新的第一步是搞清楚两件事：

- **现在在哪个分支**（或是不是 detached HEAD）？
- **应该更新到哪个分支**（用户指定的分支，还是 origin 的默认分支）？

`update-package.sh` 用一对小函数回答它们。理解它们需要两个 git 基础：

- `symbolic-ref HEAD`：当 HEAD 指向某个分支时返回 `refs/heads/<branch>`；处于「分离头指针（detached HEAD）」状态时返回空且退出码非 0。
- `refs/remotes/origin/HEAD`：克隆时 git 会建立一个符号引用，指向 origin 的默认分支，形如 `refs/remotes/origin/master`。`git_default_branch` 正是读它来「知道默认分支是谁」。

#### 4.2.2 核心流程

两个检测函数的返回约定一致——用**不同的退出码**区分三种状态：

```
git_current_branch / git_default_branch
        │
        ├─ 不在 git 仓库（git rev-parse 失败） → 返回 2，不输出
        ├─ 处于 detached HEAD / origin/HEAD 缺失 → 返回 1，输出空
        └─ 正常 → 返回 0，echo 分支名
```

主流程据此判断「当前分支是否需要切换」：

```
current_branch = git_current_branch()        # $? > 1 表示根本不是 git 仓库 → 跳过
        │
        ▼
target_branch = 用户给了 branch ? branch : git_default_branch()
        │
        ▼
current_branch != target_branch ?  ──是──▶ switch_branch()   # 切到目标分支
        │ 否                                                  （内部会 fetch_all_branches）
        ▼
（进入 4.3 的 fetch + ff-only 合并）
```

#### 4.2.3 源码精读

**当前分支检测**，用退出码 2/1/0 三态：

[scripts/update-package.sh:17-31](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L17-L31)：

```bash
git_current_branch() {
    if ! command git rev-parse 2> /dev/null; then   # 不是 git 仓库
        return 2
    fi
    local ref="$(command git symbolic-ref HEAD 2> /dev/null)"
    if [[ -n "$ref" ]]; then
        echo "${ref#refs/heads/}"                    # 剥掉 refs/heads/ 前缀
        return 0
    else
        return 1                                     # detached HEAD
    fi
}
```

注意 `command git` 的写法：`command` 会绕过任何名为 `git` 的 shell 函数或别名，确保调到真正的 git 可执行文件。`${ref#refs/heads/}` 是参数展开的「去前缀」用法，把 `refs/heads/master` 变成 `master`。

**默认分支检测**，读 `origin/HEAD`：

[scripts/update-package.sh:33-46](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L33-L46)：

```bash
git_default_branch() {
    if ! command git rev-parse 2> /dev/null; then
        return 2
    fi
    local ref="$(command git symbolic-ref refs/remotes/origin/HEAD 2> /dev/null)"
    if [[ -n "$ref" ]]; then
        echo "${ref#refs/remotes/origin/}"           # 剥掉 refs/remotes/origin/ 前缀
        return 0
    else
        return 1
    fi
}
```

它依赖克隆时建立的 `origin/HEAD` 符号引用；若该引用缺失（比如仓库被手动动过），返回 1，主流程拿到的 `target_branch` 为空。

**主流程如何接住退出码**：

[scripts/update-package.sh:73-82](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L73-L82)：

```bash
current_branch="$(git_current_branch)"
if [[ $? -gt 1 ]]; then                              # 仅 == 2（非 git 仓库）才跳过
    echo $(warning 'WARNING:') "not a git repository, skipped updating '${package_dir}'"
    exit
fi
if [[ -z "${branch}" ]]; then
    target_branch="$(git_default_branch)"            # 用户没指定分支 → 用默认分支
else
    target_branch="${branch}"
fi
```

这里的 `$?` 紧跟在赋值语句后，捕获的是命令替换 `$(git_current_branch)` 的退出码——这正是「用返回码区分三态」能工作的原因：只有「非 git 仓库」（返回 2）才 `> 1` 触发跳过；detached HEAD（返回 1）会继续往下走，`current_branch` 为空，随后与 `target_branch` 不相等而进入 `switch_branch`。

#### 4.2.4 代码实践

**目标**：在三种状态下观察 `git_current_branch` 的返回值与输出，**不需要联网**。

**步骤**：把函数单独抽到一个临时脚本里（因为 `update-package.sh` 整体 `source` 会执行主流程，不便直接测）：

```bash
cat > /tmp/gittest.sh <<'EOF'
git_current_branch() {
    if ! command git rev-parse 2> /dev/null; then return 2; fi
    local ref="$(command git symbolic-ref HEAD 2> /dev/null)"
    if [[ -n "$ref" ]]; then echo "${ref#refs/heads/}"; return 0; else return 1; fi
}
EOF

rm -rf /tmp/gtrepo && mkdir /tmp/gtrepo && cd /tmp/gtrepo && git init -q
git commit --allow-empty -qm init

# 场景 A：正常分支
source /tmp/gittest.sh; b="$(git_current_branch)"; echo "A: rc=$? branch=$b"

# 场景 B：detached HEAD
git checkout -q HEAD~0 2>/dev/null; git checkout -q --detach
source /tmp/gittest.sh; b="$(git_current_branch)"; echo "B: rc=$? branch=[$b]"

# 场景 C：非 git 仓库
cd /tmp; source /tmp/gittest.sh; b="$(git_current_branch)"; echo "C: rc=$? branch=[$b]"
```

**需要观察的现象**：A 行 `rc=0 branch=master`（或 `main`）；B 行 `rc=1 branch=[]`（空）；C 行 `rc=2 branch=[]`。

**预期结果**（据源码推断）：退出码依次为 0、1、2，与函数的三态约定一致。本机 git 默认分支名以实际为准（`master` 或 `main`）。

#### 4.2.5 小练习与答案

1. **问**：`git_current_branch` 为什么用「返回 2」而不是「返回 1」表示「不是 git 仓库」？
   **答**：因为 detached HEAD 已经占用了「返回 1」。主流程靠 `$? -gt 1` 区分——只在「非仓库（2）」时跳过，detached HEAD（1）仍允许进入切分支逻辑。

2. **问**：`git_default_branch` 如果返回空字符串，主流程会怎样？
   **答**：`target_branch` 变为空。若此时 `current_branch` 也非空（正常分支），二者不等会进入 `switch_branch ""`，随后的 `git checkout ""` 大概率失败并以 `exit 1` 退出（[update-package.sh:68](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L68)）。

3. **问**：为什么两个函数都用 `command git` 而不是直接 `git`？
   **答**：`command` 绕过可能存在的同名 shell 函数/别名，保证调用真正的 git 二进制，避免环境里被重定义的 `git` 干扰检测。

---

### 4.3 ff-only 合并与硬重置回退（主更新逻辑）

#### 4.3.1 概念说明

当「当前分支 == 目标分支」、无需切分支时，`update-package.sh` 进入真正的更新动作。它采用一条**先礼后兵**的容错链：

1. `git fetch`：把 origin 的最新提交（连同子模块）拉到远程跟踪分支 `origin/<branch>`，**不动工作区**。
2. `git merge --ff-only origin/<branch>`：尝试**纯快进**合并——只有当本地 HEAD 是 `origin/<branch>` 的直接祖先时才成功，绝不产生合并提交。
3. 一旦 ff-only 失败（本地与 origin 已经分叉，或有冲突），**立刻 `git reset --hard origin/<branch>`**，把工作区整体对齐到 origin。

第 3 步之所以「敢」硬重置、直接丢弃本地改动，根本原因仍是：**`package/` 是一次性缓存**。plum 要的是「origin 的最新文件」，不是「你在缓存里的手工修改」。这条回退路径保证了无论 `package/` 被改得多乱，下一次更新都能自愈回 origin 的干净状态。

#### 4.3.2 核心流程

主更新逻辑（[update-package.sh:83-93](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L83-L93)）的决策树：

```
current_branch == target_branch ?
        │
        ├─ 否 ──▶ switch_branch(target_branch)          # 4.2 已讲
        │
        └─ 是 ──▶ no_update 开着吗？
                  │
                  ├─ 是 ──▶ 什么都不做（仅上层报 "Found package"）
                  │
                  └─ 否 ──▶ git fetch --recurse-submodules
                            │
                            ▼
                            git merge --ff-only origin/<branch>
                            │
                            ├─ 成功(含 Already up to date) ──▶ 完成
                            │
                            └─ 失败 ──▶ 打印 WARNING
                                        git reset --hard origin/<branch>
```

`switch_branch` 自身也有一个小细节值得注意：因为 `fetch-package.sh` 用 `--depth 1` 做**单分支浅克隆**，本地往往只有当前分支的 ref，要 `checkout` 到另一个分支必须先把各分支 ref 拉下来——这正是 `fetch_all_branches` 的职责（见 4.3.3）。

#### 4.3.3 源码精读

**单分支浅克隆的补救——`fetch_all_branches`**：

[scripts/update-package.sh:48-59](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L48-L59)：

```bash
fetch_all_branches() {
    local fetch_all_pattern='\+refs/heads/\*:'
    if ! [[ "$(git config --get remote.origin.fetch)" =~ $fetch_all_pattern ]]; then
        if [[ -n "${option_no_update}" ]]; then
            echo $(warning 'WARNING:') 'forced update for switching branch to' $(print_option "${target_branch}")
        fi
        git config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
    elif [[ -n "${option_no_update}" ]]; then
        return
    fi
    git fetch origin --depth 1
}
```

逻辑：检查当前 `remote.origin.fetch` refspec 是否已是「拉所有分支」(`+refs/heads/*:`)。若不是（典型的单分支浅克隆），就改写成全分支 refspec，再 `git fetch` 把目标分支的 ref 取下来；若已是全分支**且**处在 `no_update` 模式，则直接 `return` 跳过这次 fetch。注意：即便 `no_update`，第一次切分支仍会强制 fetch（并打印 WARNING），因为没有 ref 就无法 checkout。

**切分支**：

[scripts/update-package.sh:61-69](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L61-L69)：

```bash
switch_branch() {
    local target_branch="$1"
    if [[ -z "${branch}" ]]; then          # 用户没指定分支，却不在默认分支上 → 提醒
        echo $(warning 'WARNING:') "'${package_dir}' was on" \
             $(print_option "${current_branch:-(detached HEAD)}") 'instead of' $(print_option "${target_branch}")
    fi
    fetch_all_branches
    git checkout "${target_branch}" || exit 1
}
```

`${current_branch:-(detached HEAD)}` 是参数展开的「默认值」用法：`current_branch` 为空时显示 `(detached HEAD)`。

**核心：fetch + ff-only，失败则硬重置**：

[scripts/update-package.sh:85-93](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L85-L93)：

```bash
elif [[ -z "${option_no_update}" ]]; then
    git fetch --recurse-submodules && (
        git merge --ff-only "origin/${target_branch}" || (
            echo $(warning 'WARNING:') 'fast-forward failed;' \
                 'doing a hard reset to' $(print_option "origin/${target_branch}")
            git reset --hard "origin/${target_branch}"
        )
    ) || exit 1
fi
```

这组嵌套的 `&&` / `||` / `( ... )` 读法：

- 先 `git fetch --recurse-submodules`；成功才进入括号。
- 括号里先试 `git merge --ff-only origin/<branch>`；**失败**才进入内层括号：打印 WARNING 并 `git reset --hard`。
- 外层 `|| exit 1` 兜底：若 fetch 本身失败，整个脚本以非 0 退出。

为什么 `merge --ff-only` 会失败？典型情形是「本地提交与 origin 新提交**分叉**」——本地既不是 origin 的祖先，也不是其后代。这时 fast-forward 不可能，ff-only 直接报错而非强行合并，于是落入硬重置，把本地整体重置为 `origin/<branch>`，丢弃分叉的本地提交与工作区改动。

#### 4.3.4 代码实践

**目标**：亲手制造一次「本地与 origin 分叉」，观察 `merge --ff-only` 失败 → 硬重置回退的完整过程。这是本讲的核心实践，建议在第 5 节综合实践里一并完成；此处先理解关键观察点。

**关键步骤摘录**（完整命令见第 5 节）：

```bash
# 进入一个已克隆的包目录（浅克隆，只有 1 个提交）
cd <package_dir>
git fetch --deepen 3            # ① 关键：浅克隆没有父提交，必须先"挖深"才能回退
git reset --hard HEAD~1         # ② 退到父提交 B（origin/<branch> 仍指向 C）
echo ";; local hack" >> default.yaml
git add -A && git commit -qm "local divergent"   # ③ 产出与 origin 分叉的本地提交 D
# ④ 运行 update-package.sh：<package_dir>，观察 WARNING 与 reset
```

**需要观察的现象**：运行 `update-package.sh` 后，终端出现黄色 `WARNING: fast-forward failed; doing a hard reset to origin/<branch>`；事后 `default.yaml` 末尾的 `;; local hack` 消失，`git log` 回到 origin 的提交。

**预期结果**（据源码推断）：因为本地 D 与 origin C 以 B 为共同祖先而分叉，`merge --ff-only` 必然失败，随即 `reset --hard origin/<branch>` 把工作区恢复成 origin 状态，本地提交 D 被丢弃。注意步骤 ①——`--depth 1` 下 `HEAD~1` 原本不存在，必须先 `--deepen`，这正呼应了 4.1 讲的浅克隆特性。**待本地验证**。

#### 4.3.5 小练习与答案

1. **问**：为什么更新用 `git merge --ff-only` 而不是普通 `git merge`？
   **答**：ff-only 保证不产生合并提交、不引入合并冲突的交互；一旦不能快进就立刻失败，由随后的硬重置统一兜底。普通 merge 可能留下合并提交或卡在冲突里，与「`package/` 是一次性缓存」的设计相悖。

2. **问**：若 `package/` 目录里的包**领先** origin（本地多了提交，但未分叉，origin 是本地祖先），`merge --ff-only origin/<branch>` 会怎样？
   **答**：此时 origin 已包含在本地历史中，ff-only 合并是 no-op，git 报 `Already up to date.` 并**成功返回**，本地提交被保留——并不会触发硬重置。硬重置只在「真正分叉」时发生。

3. **问**：`fetch_all_branches` 里 `git config remote.origin.fetch '+refs/heads/*:...'` 这一步解决了什么问题？
   **答**：`fetch-package.sh` 的 `--depth 1` 浅克隆通常只带「当前分支」的 refspec，本地没有其它分支的 ref，直接 `git checkout <其它分支>` 会失败。把 refspec 改写成「拉所有分支」并 fetch 后，目标分支的 ref 才可用，checkout 才能成功。

---

## 5. 综合实践

把 fetch 与 update 串成一次完整的「下载 → 改乱 → 自愈」闭环。**需要联网**（要从 GitHub 克隆真实包）。

**实践目标**：用 `fetch-package.sh` 克隆一个官方包，人为把它的 `package/` 缓存改到与 origin 分叉，再用 `update-package.sh` 验证它能通过 `fetch + ff-only 失败 + reset --hard` 恢复到 origin 状态。

**操作步骤**（请把 `<plum>` 换成你的 plum 工作副本路径，例如 `/home/you/rime-plum`）：

```bash
cd <plum>

# 1) 清场并浅克隆一个体积小的官方包到临时目录
rm -rf /tmp/plum-demo && mkdir -p /tmp/plum-demo
bash scripts/fetch-package.sh prelude /tmp/plum-demo/prelude
#    预期执行：git clone --depth 1 --recurse-submodules [--shallow-submodules] \
#              https://github.com/rime/rime-prelude.git /tmp/plum-demo/prelude

# 2) 确认它现在是个浅克隆（只有 1 个提交），并记录默认分支
cd /tmp/plum-demo/prelude
git log --oneline                               # 只看得到 1 条提交
git symbolic-ref refs/remotes/origin/HEAD       # 看默认分支，记为 <B>（通常是 master）

# 3) 制造"本地与 origin 分叉"
git fetch --deepen 3                            # 浅克隆没有父提交，先挖深
git log --oneline -4                            # 现在能看到几条历史
git reset --hard HEAD~1                         # 退到父提交 P（origin/<B> 仍指向原 tip C）
echo ";; local hack from tutorial" >> default.yaml
git add -A && git commit -qm "local divergent commit"
#   此刻：本地 <B> 在新提交 D，origin/<B> 在 C，D 与 C 以 P 为共同祖先 → 已分叉

# 4) 运行 update-package.sh，观察自愈过程
cd <plum>
bash scripts/update-package.sh /tmp/plum-demo/prelude

# 5) 验证恢复结果
cd /tmp/plum-demo/prelude
git log --oneline -3
tail -n 3 default.yaml                          # ";; local hack ..." 应已消失
```

**需要观察的现象**：

- 步骤 1 的克隆命令里确实带了 `--depth 1 --recurse-submodules`（git ≥ 2.9 还会带 `--shallow-submodules`）。
- 步骤 2 只能看到 1 条提交，证明是浅克隆。
- 步骤 4 的输出里出现黄色 `WARNING: fast-forward failed; doing a hard reset to origin/<B>`。
- 步骤 5 的 `git log` 回到 origin 的提交 C（本地提交 D 不见了），`default.yaml` 末尾的本地篡改被清除。

**预期结果**（据源码推断）：因为步骤 3 让本地与 origin 真正分叉，[update-package.sh:86-92](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L86-L92) 的 `git merge --ff-only` 必然失败，落入 `git reset --hard origin/<B>`，把 `package/prelude` 整体恢复成 origin 状态。这正好印证「`package/` 是一次性缓存、可被随时丢弃重建」的设计。完整运行需联网克隆 rime-prelude，**待本地验证**。

> **进阶尝试**：在步骤 4 之前额外执行 `no_update=1 bash scripts/update-package.sh /tmp/plum-demo/prelude`，对比输出。你会发现此时只打印「Found package」类的提示、不发网络请求（因为 [update-package.sh:85](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L85) 的 `[[ -z "${option_no_update}" ]]` 为假，整个 fetch+merge 分支被跳过），本地分叉**不会被修正**——这验证了 `no_update` 的语义。

## 6. 本讲小结

- `fetch-package.sh` 是「下载」分支：用自带的 `resolve_package_name` 把包名还原成 `https://github.com/<user>/<repo>.git`，再以 `--depth 1 --recurse-submodules`（git≥2.9 追加 `--shallow-submodules`）浅克隆，因为 `package/` 只需最新一份文件。
- `update-package.sh` 是「更新」分支：先用 `git_current_branch` / `git_default_branch`（返回码 2/1/0 三态）判断当前与目标分支；不一致则 `switch_branch`，其间用 `fetch_all_branches` 补救单分支浅克隆缺 ref 的问题。
- 同分支更新走 `git fetch --recurse-submodules` + `git merge --ff-only origin/<branch>`；ff-only 失败则 `git reset --hard origin/<branch>` 硬重置。
- 硬重置之所以可接受，是因为 `package/` 本就是一次性缓存——这条回退路径让缓存无论被改多乱都能自愈回 origin。
- `no_update` 环境变量会跳过整个 fetch+merge 分支，仅上层报「Found package」。
- 浅克隆的 `--depth 1` 带来一个副作用：本地没有父提交，需要 `git fetch --deepen N` 才能回退历史——综合实践里专门验证了这一点。

## 7. 下一步学习建议

- 顺着 install 主链路继续往下：fetch/update 把源码备好后，`install_package` 如何决定「跑配方」还是「直接拷文件」？这正是 [u2-l6 配方执行引擎 recipe.sh](u2-l6-recipe-engine.md) 的主题，建议紧接着读。
- 若想从更高层把「target 字符串 → 这两个脚本被调用」的链路补全，可回头对照 [u2-l2 配方字符串解析 resolver.sh](u2-l2-recipe-string-resolver.md) 的 `resolve_branch`，看 `@branch` 是如何变成传入本讲的 `<branch>` 参数的。
- 对 git 容错手法感兴趣的话，可在本机用 `git merge --ff-only` 与 `git reset --hard` 多构造几种场景（领先、落后、分叉），体会 plum 选择 ff-only + 硬重置而非普通 merge 的工程权衡。
