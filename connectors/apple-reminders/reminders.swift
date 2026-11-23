// Напоминания macOS для брифинга и протоколов созвонов.
// Запуск только через reminders.sh (open -W -a "Briefing Reminders.app" --args ...):
//   list <дней_вперёд> <файл_json>   чтение: открытые задачи всех списков со сроком до +N дней,
//                                    просроченные и без срока из списков агента
//   add <вход_json> <файл_json>      добавление задач. Писать можно ТОЛЬКО в списки из ALLOWED_LISTS;
//                                    задачи не меняются и не удаляются. Повтор с тем же key пропускается.
//   done <дней_назад> <файл_json>    чтение: задачи, выполненные за последние N дней
//   complete <вход_json> <файл_json> отметить выполненными по просьбе владельца из бота: {"items":[{"title","list"?}]}.
//                                    Только открытые задачи с точно таким заголовком; если таких несколько и список
//                                    не указан, отказ (ambiguous). Ничего не удаляет, галочку можно снять вручную.
//                                    "keep_one": true со списком закрывает дубли, оставляя самую раннюю.
//   retime <вход_json> <файл_json>   время и напоминание задачам со сроком сегодня: {"items":[{"title","list"?,"time"?}]}
// Вход add: {"items":[{"list","title","notes","due":"ГГГГ-ММ-ДД","time":"ЧЧ:ММ","key","priority":"high"?}]}
import EventKit
import Foundation

// Список-уведомление о готовом брифинге: в list не попадает, при новом add старые брифинги
// (только этого списка и только с key brief:) отмечаются выполненными, чтобы не копились.
let BRIEF_LIST = "Брифинг"
let ALLOWED_LISTS = ["Из созвонов", "Из переписки", "Жду от других", "Подготовка к встречам", "Срочно", "Оплаты", "Дни рождения", BRIEF_LIST]
let KEY_PREFIX = "key: "

let args = CommandLine.arguments
let command = args.count > 1 ? args[1] : ""
let outPath = args.count > 3 ? args[3] : nil

func finish(_ object: Any, to path: String?, code: Int32 = 0) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])) ?? Data()
    if let path { try? data.write(to: URL(fileURLWithPath: path)) }
    FileHandle.standardOutput.write(data)
    exit(code)
}

let store = EKEventStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
store.requestFullAccessToReminders { ok, _ in granted = ok; sem.signal() }
sem.wait()
guard granted else {
    finish(["error": "Нет доступа к Напоминаниям: Системные настройки, Конфиденциальность и безопасность, Напоминания, Briefing Reminders"],
           to: outPath, code: 2)
}

let cal = Calendar.current
let dayFmt = DateFormatter(); dayFmt.dateFormat = "yyyy-MM-dd"; dayFmt.timeZone = .current
let timeFmt = DateFormatter(); timeFmt.dateFormat = "HH:mm"; timeFmt.timeZone = .current

func fetch(_ predicate: NSPredicate) -> [EKReminder] {
    var result: [EKReminder] = []
    let s = DispatchSemaphore(value: 0)
    store.fetchReminders(matching: predicate) { r in result = r ?? []; s.signal() }
    s.wait()
    return result
}

func describe(_ r: EKReminder, today: Date) -> [String: Any] {
    var item: [String: Any] = ["list": r.calendar.title, "title": r.title ?? ""]
    if let dc = r.dueDateComponents, let due = cal.date(from: dc) {
        item["due"] = dayFmt.string(from: due)
        if dc.hour != nil { item["time"] = timeFmt.string(from: due) }
        let days = cal.dateComponents([.day], from: cal.startOfDay(for: due), to: today).day ?? 0
        if days > 0 { item["overdue_days"] = days }
    }
    if let notes = r.notes, !notes.isEmpty {
        item["notes"] = notes.split(separator: "\n").filter { !$0.hasPrefix(KEY_PREFIX) }.joined(separator: "\n")
    }
    if r.priority > 0 { item["priority"] = r.priority <= 4 ? "высокий" : (r.priority == 5 ? "средний" : "низкий") }
    return item
}

switch command {
case "list":
    let daysAhead = args.count > 2 ? Double(args[2]) ?? 7 : 7
    let path = args.count > 3 ? args[3] : nil
    let today = cal.startOfDay(for: Date())
    let horizon = cal.date(byAdding: .day, value: Int(daysAhead) + 1, to: today)!
    let lists = store.calendars(for: .reminder)
    let open = fetch(store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: lists))
    var dated: [[String: Any]] = [], undated: [[String: Any]] = []
    for r in open where r.calendar.title != BRIEF_LIST {
        if let dc = r.dueDateComponents, let due = cal.date(from: dc) {
            if due < horizon { dated.append(describe(r, today: today)) }
        } else if ALLOWED_LISTS.contains(r.calendar.title) {
            undated.append(describe(r, today: today))
        }
    }
    dated.sort { ($0["due"] as? String ?? "") < ($1["due"] as? String ?? "") }
    var counts: [String: Int] = [:]
    for r in open { counts[r.calendar.title, default: 0] += 1 }
    finish(["today": dayFmt.string(from: today), "due": dated, "agent_lists_undated": undated,
            "open_by_list": counts], to: path)

case "done":
    let daysBack = args.count > 2 ? Int(args[2]) ?? 7 : 7
    let path = args.count > 3 ? args[3] : nil
    let today = cal.startOfDay(for: Date())
    let from = cal.date(byAdding: .day, value: -daysBack, to: today)!
    let completed = fetch(store.predicateForCompletedReminders(withCompletionDateStarting: from, ending: Date(),
                                                               calendars: store.calendars(for: .reminder)))
    let items: [[String: Any]] = completed
        .sorted { ($0.completionDate ?? .distantPast) < ($1.completionDate ?? .distantPast) }
        .map { r in
            var item = describe(r, today: today)
            item.removeValue(forKey: "overdue_days")
            if let d = r.completionDate { item["completed"] = dayFmt.string(from: d) }
            return item
        }
    finish(["from": dayFmt.string(from: from), "completed": items], to: path)

case "add":
    guard args.count > 3, let input = try? Data(contentsOf: URL(fileURLWithPath: args[2])),
          let json = try? JSONSerialization.jsonObject(with: input) as? [String: Any],
          let items = json["items"] as? [[String: Any]] else {
        finish(["error": "Вход: add <вход_json> <файл_json>, формат {\"items\":[…]}"], to: args.count > 3 ? args[3] : nil, code: 1)
    }
    let path = args[3]
    guard let source = store.defaultCalendarForNewReminders()?.source else {
        finish(["error": "Нет источника для новых списков Напоминаний (iCloud)"], to: path, code: 1)
    }
    var created: [String] = [], skipped: [String] = [], refused: [String] = []
    var listCache: [String: EKCalendar] = [:]
    func list(named name: String) throws -> EKCalendar {
        if let c = listCache[name] { return c }
        if let c = store.calendars(for: .reminder).first(where: { $0.title == name }) { listCache[name] = c; return c }
        let c = EKCalendar(for: .reminder, eventStore: store)
        c.title = name
        c.source = source
        try store.saveCalendar(c, commit: true)
        listCache[name] = c
        return c
    }
    for item in items {
        let title = (item["title"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let listName = item["list"] as? String ?? ALLOWED_LISTS[0]
        guard !title.isEmpty, ALLOWED_LISTS.contains(listName) else { refused.append("\(listName): \(title)"); continue }
        do {
            let target = try list(named: listName)
            let key = item["key"] as? String ?? ""
            if !key.isEmpty {
                let existing = fetch(store.predicateForReminders(in: [target]))
                if existing.contains(where: { ($0.notes ?? "").contains(KEY_PREFIX + key) }) { skipped.append(title); continue }
            }
            if listName == BRIEF_LIST && key.hasPrefix("brief:") {
                let old = fetch(store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: [target]))
                for o in old where (o.notes ?? "").contains(KEY_PREFIX + "brief:") {
                    o.isCompleted = true
                    try? store.save(o, commit: true)
                }
            }
            let r = EKReminder(eventStore: store)
            r.calendar = target
            r.title = title
            if (item["priority"] as? String) == "high" { r.priority = 1 }
            var notes = item["notes"] as? String ?? ""
            if !key.isEmpty { notes += (notes.isEmpty ? "" : "\n") + KEY_PREFIX + key }
            r.notes = notes.isEmpty ? nil : notes
            if let dueStr = item["due"] as? String, let day = dayFmt.date(from: dueStr) {
                var dc = cal.dateComponents([.year, .month, .day], from: day)
                let timeStr = item["time"] as? String ?? ""
                let parts = timeStr.split(separator: ":").compactMap { Int($0) }
                let alarmHour = parts.count == 2 ? parts[0] : 10, alarmMinute = parts.count == 2 ? parts[1] : 0
                if parts.count == 2 { dc.hour = parts[0]; dc.minute = parts[1] }
                r.dueDateComponents = dc
                if var alarmDate = cal.date(bySettingHour: alarmHour, minute: alarmMinute, second: 0, of: day) {
                    // без времени напоминание в 10:00, но если 10:00 уже прошло (задача после встречи), через 5 минут
                    let soon = Date().addingTimeInterval(5 * 60)
                    if parts.count != 2 && alarmDate < soon && cal.isDate(day, inSameDayAs: Date()) { alarmDate = soon }
                    r.addAlarm(EKAlarm(absoluteDate: alarmDate))
                }
            }
            try store.save(r, commit: true)
            created.append(title)
        } catch {
            refused.append("\(title): \(error.localizedDescription)")
        }
    }
    finish(["created": created, "skipped_duplicates": skipped, "refused": refused], to: path)

case "complete":
    guard args.count > 3, let input = try? Data(contentsOf: URL(fileURLWithPath: args[2])),
          let json = try? JSONSerialization.jsonObject(with: input) as? [String: Any],
          let items = json["items"] as? [[String: Any]] else {
        finish(["error": "Вход: complete <вход_json> <файл_json>, формат {\"items\":[{\"title\",\"list\"}]}"], to: args.count > 3 ? args[3] : nil, code: 1)
    }
    let path = args[3]
    let open = fetch(store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil,
                                                          calendars: store.calendars(for: .reminder)))
    func norm(_ t: String) -> String { t.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
    var completed: [[String: String]] = [], notFound: [String] = [], ambiguous: [[String: Any]] = []
    for item in items.prefix(10) {
        let title = item["title"] as? String ?? ""
        let listName = item["list"] as? String
        let matches = open.filter { norm($0.title ?? "") == norm(title) && (listName == nil || $0.calendar.title == listName) && !$0.isCompleted }
        if matches.isEmpty { notFound.append(title); continue }
        let keepOne = (item["keep_one"] as? Bool) == true && listName != nil
        if matches.count > 1 && !keepOne { ambiguous.append(["title": title, "lists": matches.map { $0.calendar.title }]); continue }
        // keep_one: закрыть дубли (одинаковый заголовок в одном списке), самую раннюю оставить
        let targets = keepOne ? Array(matches.sorted { ($0.creationDate ?? .distantPast) < ($1.creationDate ?? .distantPast) }.dropFirst()) : matches
        for r in targets {
            r.isCompleted = true
            do { try store.save(r, commit: true); completed.append(["title": r.title ?? "", "list": r.calendar.title]) }
            catch { notFound.append("\(title): \(error.localizedDescription)") }
        }
    }
    finish(["completed": completed, "not_found": notFound, "ambiguous": ambiguous], to: path)

case "retime":
    // {"items":[{"title","list"?,"time":"ЧЧ:ММ"?}]}: открытым задачам со сроком сегодня поставить время и напоминание;
    // без time через 5 минут. Заголовок, заметки и срок не меняются.
    guard args.count > 3, let input = try? Data(contentsOf: URL(fileURLWithPath: args[2])),
          let json = try? JSONSerialization.jsonObject(with: input) as? [String: Any],
          let items = json["items"] as? [[String: Any]] else {
        finish(["error": "Вход: retime <вход_json> <файл_json>, формат {\"items\":[{\"title\",\"list\",\"time\"}]}"], to: args.count > 3 ? args[3] : nil, code: 1)
    }
    let path = args[3]
    let now = Date()
    let open = fetch(store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil,
                                                          calendars: store.calendars(for: .reminder)))
    func normT(_ t: String) -> String { t.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
    var changed: [[String: String]] = [], notFound: [String] = []
    for item in items.prefix(20) {
        let title = item["title"] as? String ?? ""
        let listName = item["list"] as? String
        let matches = open.filter { r in
            guard normT(r.title ?? "") == normT(title), listName == nil || r.calendar.title == listName,
                  let dc = r.dueDateComponents, let due = cal.date(from: dc) else { return false }
            return cal.isDate(due, inSameDayAs: now)
        }
        if matches.isEmpty { notFound.append(title); continue }
        let parts = (item["time"] as? String ?? "").split(separator: ":").compactMap { Int($0) }
        let at = parts.count == 2 ? (cal.date(bySettingHour: parts[0], minute: parts[1], second: 0, of: now) ?? now)
                                  : now.addingTimeInterval(5 * 60)
        for r in matches {
            var dc = cal.dateComponents([.year, .month, .day, .hour, .minute], from: at)
            dc.second = nil
            r.dueDateComponents = dc
            for a in r.alarms ?? [] { r.removeAlarm(a) }
            r.addAlarm(EKAlarm(absoluteDate: at))
            do { try store.save(r, commit: true); changed.append(["title": r.title ?? "", "list": r.calendar.title, "time": timeFmt.string(from: at)]) }
            catch { notFound.append("\(title): \(error.localizedDescription)") }
        }
    }
    finish(["changed": changed, "not_found": notFound], to: path)

default:
    finish(["error": "Команды: list <дней> <файл> | done <дней> <файл> | add <вход> <файл> | complete <вход> <файл>"], to: args.count > 2 ? args.last : nil, code: 1)
}
