// UnixLoadConstr.h
// This is the dynamic loading implementation for UNIX machines
#if !defined(_UNIX_LOAD_CONSTR_H)
#define _UNIX_LOAD_CONSTR_H

#include <dlfcn.h>
#include <iostream>
#include <string>

#include "suffix.h"
#include "CycException.h"
#include "Env.h"

//- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
mdl_ctor* Model::loadConstructor(std::string model_type, std::string model_name) {
  mdl_ctor* new_model;

  std::string start_path = Env::getInstallPath() + "/lib"  ;

  std::string construct_fname = std::string("construct") + model_name;
  //  std::string destruct_fname = std::string("destruct") + model_name;

  model_name = start_path + "/Models/" + model_type + "/" + 
    model_name + "/lib" + model_name+SUFFIX;
  void* model = dlopen(model_name.c_str(),RTLD_LAZY);
  if (!model) {
    std::string err_msg = "Unable to load model shared object file: ";
    err_msg += dlerror();
    throw CycIOException(err_msg);
  }

  dynamic_libraries_.push_back(model);

  new_model = (mdl_ctor*) dlsym(model, construct_fname.c_str() );
  if (!new_model) {
    std::string err_msg = "Unable to load model constructor: ";
    err_msg += dlerror();
    throw CycIOException(err_msg);
  }
  
  return new_model;
}

//- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
void Model::closeDynamicLibraries() {
  while (!dynamic_libraries_.empty()) {
    void* lib = dynamic_libraries_.back();
    dlclose(lib);
    dynamic_libraries_.pop_back();
  }
}
#endif
