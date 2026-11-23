// Встречи из Календаря macOS (все подключённые календари, в том числе Exchange).
// Только чтение. Использование: open -W -a "Briefing Calendar.app" --args <часов_вперёд> <часов_назад> <файл_json> [birthdays]
// С birthdays выводит только дни рождения из Контактов.
// Вывод: JSON {range, calendars, events}; события отсортированы по времени начала.
import EventKit
import Foundation

let args = CommandLine.arguments
let hoursAhead = args.count > 1 ? Double(args[1]) ?? 40 : 40
let hoursBack = args.count > 2 ? Double(args[2]) ?? 0 : 0

let store = EKEventStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
store.requestFullAccessToEvents { ok, _ in granted = ok; sem.signal() }
sem.wait()
guard granted else {
    let msg = "{\"error\": \"Нет доступа к Календарю: Системные настройки, Конфиденциальность и безопасность, Календари, Briefing Calendar\"}"
    if let path = ProcessInfo.processInfo.environment["BRIEFING_OUT"] ?? (args.count > 3 ? args[3] : nil) {
        try? msg.write(toFile: path, atomically: true, encoding: .utf8)
    }
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(2)
}

let cal = Calendar.current
let from = hoursBack > 0 ? Date().addingTimeInterval(-hoursBack * 3600) : cal.startOfDay(for: Date())
let to = Date().addingTimeInterval(hoursAhead * 3600)
let onlyBirthdays = args.count > 4 && args[4] == "birthdays"  // режим дней рождения из Контактов
let calendars = store.calendars(for: .event).filter { onlyBirthdays ? $0.type == .birthday : $0.type != .birthday }
let events = store.events(matching: store.predicateForEvents(withStart: from, end: to, calendars: calendars))
    .sorted { $0.startDate < $1.startDate }

let iso = ISO8601DateFormatter()
iso.timeZone = TimeZone.current
iso.formatOptions = [.withInternetDateTime]

func statusName(_ s: EKParticipantStatus) -> String {
    switch s {
    case .accepted: return "принял"
    case .declined: return "отклонил"
    case .tentative: return "под вопросом"
    case .pending: return "не ответил"
    default: return ""
    }
}

let linkPattern = try! NSRegularExpression(pattern: "https?://[^\\s<>\"]+")
func links(_ text: String?) -> [String] {
    guard let t = text else { return [] }
    let r = NSRange(t.startIndex..., in: t)
    let found = linkPattern.matches(in: t, range: r)
        .compactMap { Range($0.range, in: t).map { String(t[$0]).trimmingCharacters(in: CharacterSet(charactersIn: "]>)\"'.,;")) } }
        .filter { $0.range(of: "/static/|\\.(png|jpe?g|gif|svg)(\\?|$)", options: [.regularExpression, .caseInsensitive]) == nil }
    let meeting = found.filter { $0.range(of: "zoom|teams|meet\\.google|telemost|ktalk|webex|jazz|sber|contour|kontur|yandex", options: [.regularExpression, .caseInsensitive]) != nil }
    return Array(Set(meeting.isEmpty ? Array(found.prefix(2)) : meeting)).sorted()
}

var out: [[String: Any]] = []
for e in events {
    var item: [String: Any] = [
        "start": iso.string(from: e.startDate),
        "end": iso.string(from: e.endDate),
        "all_day": e.isAllDay,
        "title": e.title ?? "",
        "calendar": e.calendar.title,
        "account": e.calendar.source.title,
        "canceled": e.status == .canceled || (e.title ?? "").range(of: "^(Canceled|Cancelled|Отменено)", options: .regularExpression) != nil,
    ]
    if let loc = e.location, !loc.isEmpty { item["location"] = loc }
    let l = links([e.location, e.url?.absoluteString, e.notes].compactMap { $0 }.joined(separator: "\n"))
    if !l.isEmpty { item["links"] = l }
    if let org = e.organizer { item["organizer"] = org.name ?? org.url.absoluteString }
    if let att = e.attendees, !att.isEmpty {
        item["attendees"] = att.map { a -> String in
            let name = a.name ?? a.url.absoluteString.replacingOccurrences(of: "mailto:", with: "")
            let st = statusName(a.participantStatus)
            return st.isEmpty ? name : "\(name) (\(st))"
        }
        if let me = att.first(where: { $0.isCurrentUser }) { item["my_status"] = statusName(me.participantStatus) }
    }
    if let n = e.notes?.trimmingCharacters(in: .whitespacesAndNewlines), !n.isEmpty { item["notes"] = String(n.prefix(600)) }
    out.append(item)
}
let result: [String: Any] = [
    "range": [iso.string(from: from), iso.string(from: to)],
    "calendars": calendars.map { "\($0.title) (\($0.source.title))" },
    "events": out,
]
let data = try! JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
// Запуск через `open` не передаёт stdout, поэтому результат пишется в файл из BRIEFING_OUT или рядом с приложением.
if let path = ProcessInfo.processInfo.environment["BRIEFING_OUT"] ?? (args.count > 3 ? args[3] : nil) {
    try! data.write(to: URL(fileURLWithPath: path))
} else {
    print(String(data: data, encoding: .utf8)!)
}
