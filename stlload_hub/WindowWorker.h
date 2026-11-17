#pragma once
#include <QJsonObject>
#include <QDir>
#include <QTemporaryDir>
#include <memory>
#include <stdexcept>
struct WindowWorkerFailure : std::runtime_error {
    QString code, message;
    QJsonObject details;
    WindowWorkerFailure(const QString& c,const QString& m,const QJsonObject& d)
        : std::runtime_error(m.toUtf8().constData()),code(c),message(m),details(d) {}
};
bool isWindowOperation(const QString& operation);
QJsonObject executeWindowOperation(const QJsonObject& request, const QDir& base,
                                  std::unique_ptr<QTemporaryDir>& run);
