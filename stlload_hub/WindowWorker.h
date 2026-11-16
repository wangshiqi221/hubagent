#pragma once
#include <QJsonObject>
#include <QDir>
#include <QTemporaryDir>
#include <memory>
bool isWindowOperation(const QString& operation);
QJsonObject executeWindowOperation(const QJsonObject& request, const QDir& base,
                                  std::unique_ptr<QTemporaryDir>& run);
